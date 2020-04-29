"""
Implementation of the collapsed gibbs sampler
for LDA where the distribution of each table are multivariate gaussian
with unknown mean and covariances.

Closely based on the authors' implementation in Java:
  https://github.com/rajarshd/Gaussian_LDA

This implementation uses Numpy/Scipy.

This is a basic implementation that implements the simple version of the training with no
speed-up tricks, and also the Cholesky decomposition, which can be disabled.

"""
import json
import math
import multiprocessing as mp
import os
import pickle
import shutil
from multiprocessing import Process
import random

import numpy as np
from gaussianlda.mp_utils import GaussianLock, SharedArray, TwoSidedLock
from numpy.core.umath import isinf
from numpy.linalg import slogdet
from scipy.linalg import solve_triangular
from scipy.special import gammaln

from gaussianlda.prior import Wishart
from gaussianlda.utils import get_logger, get_progress_bar, chol_rank1_downdate, chol_rank1_update


class GaussianLDACholTrainer_TextAudio:
    def __init__(self, corpus,audio_corpus, audio_features,vocab_embeddings, vocab,num_tables, alpha=None, kappa=0.1, log=None, save_path=None,
                 show_topics=None, mh_steps=2, num_words_for_formatting=None, das_normalization=True, show_progress=True,cholesky_decomp=True):
        """

        :param corpus:
        :param audio_feature:
        :param vocab_embeddings:
        :param vocab:
        :param num_tables:
        :param alpha: Dirichlet concentration. Defaults to 1/num_tables
        :param kappa:
        :param log:
        :param save_path:
        :param show_topics:
        :param mh_steps:
        :param num_words_for_formatting: By default, each topic is formatted by computing the probability of
            every word in the vocabulary under that topic. This can take a long time for a large vocabulary.
            If given, this limits the number considered to the first
            N in the vocabulary (which makes sense if the vocabulary is ordered with most common words first).
        :param das_normalization: Use the normalization of probability distributions used by Das, Zaheer and Dyer's
            original implementation when computing the sampling probability to choose whether to use the document
            posterior or language model part of the topic posterior. If False, do not normalize in this way, but use
            an alternative, which looks to me like it's more correct mathematically.
        """
        if log is None:
            log = get_logger("GLDA")
        self.log = log
        self.show_progress = show_progress
        # Vocab is used for outputting topics
        self.vocab = vocab
        self.show_topics = show_topics
        self.save_path = save_path

        # Dirichlet hyperparam
        if alpha is None:
            alpha = 1. / num_tables
        self.alpha = alpha

        self.das_normalization = das_normalization
        self.cholesky_decomp = cholesky_decomp
        # dataVectors
        self.vocab_embeddings = vocab_embeddings
        self.embedding_size = vocab_embeddings.shape[1]
        self.num_terms = vocab_embeddings.shape[0]
        
        self.audio_feature_size = audio_features.shape[1]
        self.audio_num_terms = audio_features.shape[0]
        # List of list of ints
        self.corpus = corpus
        self.audio_corpus = audio_corpus
        #numpy array with audio features per document (length of the array per document can be different from the number of words in the document)
        self.audio_features = audio_features
        # numIterations
        # K, num tables
        self.num_tables = num_tables
        # N, num docs
        self.num_documents = len(corpus)
        # In the current iteration, map of table_id's to number of customers. Table id starts from 0
        # Use shared memory
        self.table_counts = np.zeros((self.num_tables), dtype=np.int32)
        self.table_counts_audio = np.zeros((self.num_tables), dtype=np.int32)
        
        # K x N array.tableCounts[i][j] represents how many words of document j are present in topic i.
        self.table_counts_per_doc = np.zeros((self.num_tables, self.num_documents), dtype=np.int32)
        self.table_counts_per_doc_audio = np.zeros((self.num_tables, self.num_documents), dtype=np.int32)
        # Stores the table (topic) assignment of each customer in each iteration
        # tableAssignments[i][j] gives the table assignment of customer j of the ith document.
        self.table_assignments = []
        self.table_assignments_audio = []
        # The following 4 parameters are arraylist (list) and not maps (dict) because,
        # if they are K tables, they are continuously numbered from 0 to K-1 and hence we can directly index them
        # Mean vector associated with each table in the current iteration.
        # This is the bayesian mean (i.e has the prior part too)
        # Use shared memory
        self.table_means = np.zeros((self.num_tables, self.embedding_size), dtype=np.float64)
        self.table_means_audio = np.zeros((self.num_tables, self.audio_feature_size), dtype=np.float64)
        # log-determinant of covariance matrix for each table.
        # Since 0.5 * logDet is required in (see logMultivariateTDensity), therefore that value is kept.
        # Use shared memory
        self.log_determinants = np.zeros(self.num_tables, dtype=np.float64)
        self.log_determinants_audio = np.zeros(self.num_tables, dtype=np.float64)
        # Stores the sum of the vectors of customers at a given table
        self.sum_table_customers = np.zeros((self.num_tables, self.embedding_size), dtype=np.float64)
        self.sum_table_customers_audio = np.zeros((self.num_tables, self.audio_feature_size), dtype=np.float64)
        # Stores the squared sum of the vectors of customers at a given table
        self.sum_squared_table_customers = np.zeros((self.num_tables, self.embedding_size, self.embedding_size), dtype=np.float64)
        self.sum_squared_table_customers_audio = np.zeros((self.num_tables, self.audio_feature_size, self.audio_feature_size), dtype=np.float64)
        # Cholesky Lower Triangular Decomposition of covariance matrix associated with each table.
        # Use shared memory
        self.table_cholesky_ltriangular_mat = np.zeros(
                (self.num_tables, self.embedding_size, self.embedding_size), dtype=np.float64)
        
        self.table_cholesky_ltriangular_mat_audio = np.zeros(
                (self.num_tables, self.audio_feature_size, self.audio_feature_size), dtype=np.float64)

        # Normal inverse wishart prior
        self.prior = Wishart(self.vocab_embeddings, kappa=kappa)
        # Normal inverse wishart prior for the audio component
        self.prior_audio = Wishart(self.audio_features, kappa=kappa)
        
        # Cache k_0\mu_0\mu_0^T, only compute it once
        # Used in calculate_table_params()
        self.k0mu0mu0T = self.prior.kappa * np.outer(self.prior.mu, self.prior.mu)
        self.k0mu0mu0T_audio = self.prior.kappa * np.outer(self.prior_audio.mu, self.prior_audio.mu)
        

        self.num_words_for_formatting = num_words_for_formatting

        self.log.info("Initializing assignments")
        self.initialize()

    def initialize(self):
        """
        Initialize the gibbs sampler state.

        I start with log N tables and randomly initialize customers to those tables.

        """
        # First check the prior degrees of freedom.
        # It has to be >= num_dimension
        if self.prior.nu < self.embedding_size:
            self.log.warn("The initial degrees of freedom of the prior is less than the dimension!. "
                          "Setting it to the number of dimensions: {}".format(self.embedding_size))
            self.prior.nu = self.embedding_size
            
        if self.prior_audio.nu < self.audio_feature_size:
            self.log.warn("The initial degrees of freedom of the prior is less than the dimension!. "
                          "Setting it to the number of dimensions: {}".format(self.audio_feature_size))
            self.prior.nu = self.audio_feature_size        

        deg_of_freedom = self.prior.nu - self.embedding_size + 1
        deg_of_freedom_audio = self.prior_audio.nu - self.audio_feature_size + 1
        
        # Now calculate the covariance matrix of the multivariate T-distribution
        coeff = (self.prior.kappa + 1.) / (self.prior.kappa * deg_of_freedom)
        sigma_T = self.prior.sigma * coeff
        
        coeff_audio = (self.prior_audio.kappa + 1.) / (self.prior_audio.kappa * deg_of_freedom_audio)
        sigma_T_audio = self.prior_audio.sigma * coeff_audio
        # This features in the original code, but doesn't get used
        # Or is it just to check that the invert doesn't fail?
        #sigma_Tinv = inv(sigma_T)
        sigma_TDet_sign, sigma_TDet = slogdet(sigma_T)
        sigma_T_audioDet_sign, sigma_T_audioDet = slogdet(sigma_T_audio)
        if sigma_TDet_sign != 1:
            raise ValueError("sign of log determinant of initial sigma is {}".format(sigma_TDet_sign))
        
        if sigma_T_audioDet_sign != 1:
            raise ValueError("sign of log determinant of initial sigma is {}".format(sigma_T_audioDet_sign))

        # Storing zeros in sumTableCustomers and later will keep on adding each customer.
        self.sum_table_customers[:] = 0
        self.sum_table_customers_audio[:] = 0
        self.sum_squared_table_customers[:] = 0
        self.sum_squared_table_customers_audio[:] = 0
        
        # Means are set to the prior and then updated as we add each assignment
        self.table_means[:] = self.prior.mu
        self.table_means_audio[:] = self.prior_audio.mu

        # Initialize the cholesky decomp of each table, with no counts yet
        for table in range(self.num_tables):
            self.table_cholesky_ltriangular_mat[table] = self.prior.chol_sigma.copy()
            self.table_cholesky_ltriangular_mat_audio[table] = self.prior_audio.chol_sigma.copy()

        # Randomly assign customers to tables
        self.table_assignments = []
        self.table_assignments_audio = []
        pbar = get_progress_bar(len(self.corpus), title="Initializing", show_progress=self.show_progress)
        for doc_num, doc in enumerate(pbar(self.corpus)):
            
            audio_scene = self.audio_corpus[doc_num]
            tables = list(np.random.randint(self.num_tables, size=len(doc)))
            self.table_assignments.append(tables)
            
            tables_audio = list(np.random.randint(self.num_tables, size=audio_scene.shape[0]))
            self.table_assignments_audio.append(tables_audio)
            
            for (word, table) in zip(doc, tables):
                self.table_counts[table] += 1
                self.table_counts_per_doc[table, doc_num] += 1
                # update the sumTableCustomers
                self.sum_table_customers[table] += self.vocab_embeddings[word]
                self.sum_squared_table_customers[table] += np.outer(self.vocab_embeddings[word], self.vocab_embeddings[word])

                self.update_table_params_text(table, word)
                
            for (frame, table) in zip(audio_scene, tables_audio):
                self.table_counts_audio[table] += 1
                self.table_counts_per_doc_audio[table, doc_num] += 1
                # update the sumTableCustomers
                self.sum_table_customers_audio[table] += frame
                self.sum_squared_table_customers_audio[table] += np.outer(frame, frame)

                self.update_table_params_audio(table, frame)

    def update_table_params_audio(self, table_id, frame, is_removed=False):
            count = self.table_counts_audio[table_id]
            k_n = self.prior_audio.kappa + count
            nu_n = self.prior_audio.nu + count
            scaleTdistrn = (k_n + 1.) / (k_n * (float(nu_n) - self.audio_feature_size + 1.))

            if is_removed:
                # Now use the rank1 downdate to calculate the cholesky decomposition of the updated covariance matrix
                # The update equation is
                #   \Sigma_(N+1) =\Sigma_(N) - (k_0 + N+1) / (k_0 + N)(X_{n} - \mu_{n-1})(X_{n} - \mu_{n-1}) ^ T
                # Therefore x = sqrt((k_0 + N - 1) / (k_0 + N)) (X_{n} - \mu_{n})
                # Note here \mu_n will be the mean before updating.
                # After updating sigma_n, we will update \mu_n.

                # calculate (X_{n} - \mu_{n-1})
                # This uses the old mean, not yet updated
                x = (frame - self.table_means_audio[table_id]) * np.sqrt((k_n + 1.) / k_n)
                # The Chol rank1 downdate modifies the array in place
                
                chol_rank1_downdate(self.table_cholesky_ltriangular_mat_audio[table_id], x)

                # Update the mean
                new_mean = self.table_means_audio[table_id] * (k_n + 1.)
                new_mean -= frame
                new_mean /= k_n
                
                self.table_means_audio[table_id] = new_mean
            else:
                # New customer is added
                new_mean = self.table_means_audio[table_id] * (k_n - 1.)
                new_mean += frame
                new_mean /= k_n
                self.table_means_audio[table_id] = new_mean

                # We need to recompute det(Sig) and (v_{d,i} - mu) . Sig^-1 . (v_{d,i} - mu)
                # v_{d,i} is the word vector being added

                # The rank1 update equation is
                #  \Sigma_{n+1} = \Sigma_{n} + (k_0 + n + 1) / (k_0 + n) * (x_{n+1} - \mu_{n+1})(x_{n+1} - \mu_{n+1}) ^ T
                # calculate (X_{n} - \mu_{n-1})
                # This time we update the mean first and use the new mean
                x = (frame - self.table_means_audio[table_id]) * np.sqrt(k_n / (k_n - 1.))
                # The update modifies the decomp array in place
 
                chol_rank1_update(self.table_cholesky_ltriangular_mat_audio[table_id], x)

            # Calculate the 0.5 * log(det) + D / 2 * scaleTdistrn
            # The scaleTdistrn is because the posterior predictive distribution sends in a scaled value of \Sigma
            self.log_determinants_audio[table_id] = \
                np.sum(np.log(np.diagonal(self.table_cholesky_ltriangular_mat_audio[table_id]))) \
                + self.audio_feature_size * np.log(scaleTdistrn) / 2.


    def update_table_params_text(self, table_id, cust_id, is_removed=False):
        count = self.table_counts[table_id]
        k_n = self.prior.kappa + count
        nu_n = self.prior.nu + count
        scaleTdistrn = (k_n + 1.) / (k_n * (float(nu_n) - self.embedding_size + 1.))

        if is_removed:
            # Now use the rank1 downdate to calculate the cholesky decomposition of the updated covariance matrix
            # The update equation is
            #   \Sigma_(N+1) =\Sigma_(N) - (k_0 + N+1) / (k_0 + N)(X_{n} - \mu_{n-1})(X_{n} - \mu_{n-1}) ^ T
            # Therefore x = sqrt((k_0 + N - 1) / (k_0 + N)) (X_{n} - \mu_{n})
            # Note here \mu_n will be the mean before updating.
            # After updating sigma_n, we will update \mu_n.

            # calculate (X_{n} - \mu_{n-1})
            # This uses the old mean, not yet updated
            x = (self.vocab_embeddings[cust_id] - self.table_means[table_id]) * np.sqrt((k_n + 1.) / k_n)
            # The Chol rank1 downdate modifies the array in place
            chol_rank1_downdate(self.table_cholesky_ltriangular_mat[table_id], x)

            # Update the mean
            new_mean = self.table_means[table_id] * (k_n + 1.)
            new_mean -= self.vocab_embeddings[cust_id]
            new_mean /= k_n
            self.table_means[table_id] = new_mean
        else:
            # New customer is added
            new_mean = self.table_means[table_id] * (k_n - 1.)
            new_mean += self.vocab_embeddings[cust_id]
            new_mean /= k_n
            self.table_means[table_id] = new_mean

            # We need to recompute det(Sig) and (v_{d,i} - mu) . Sig^-1 . (v_{d,i} - mu)
            # v_{d,i} is the word vector being added

            # The rank1 update equation is
            #  \Sigma_{n+1} = \Sigma_{n} + (k_0 + n + 1) / (k_0 + n) * (x_{n+1} - \mu_{n+1})(x_{n+1} - \mu_{n+1}) ^ T
            # calculate (X_{n} - \mu_{n-1})
            # This time we update the mean first and use the new mean
            x = (self.vocab_embeddings[cust_id] - self.table_means[table_id]) * np.sqrt(k_n / (k_n - 1.))
            # The update modifies the decomp array in place
            chol_rank1_update(self.table_cholesky_ltriangular_mat[table_id], x)

        # Calculate the 0.5 * log(det) + D / 2 * scaleTdistrn
        # The scaleTdistrn is because the posterior predictive distribution sends in a scaled value of \Sigma

        self.log_determinants[table_id] = \
            np.sum(np.log(np.diagonal(self.table_cholesky_ltriangular_mat[table_id]))) \
            + self.embedding_size * np.log(scaleTdistrn) / 2.

    def calculate_table_params(self, table_id):
        """
        This method calculates the table params (bayesian mean, covariance^-1, determinant etc.)

        All it needs is the table_id and the tableCounts, tableMembers and sumTableCustomers
        should be updated correctly before calling this.

        It's used by set_table_params(), but is separated so you can calculate things without
        updating the stored values. (We don't actually do this anywhere here at the moment.)

        """
        # Total global assignments to table
        count = self.table_counts[table_id]
        k_n = self.prior.kappa + count
        nu_n = self.prior.nu + count

        # Update table mean
        mu_n = (self.sum_table_customers[table_id] + self.prior.mu * self.prior.kappa) / k_n

        # we will be using the new update
        # Sigma_N = Sigma_0 + \sum(y_iy_i^T) - (k_n)\mu_N\mu_N^T + k_0\mu_0\mu_0^T
        # calculate \mu_N\mu_N^T
        mu_n_mu_nT = np.outer(mu_n, mu_n) * k_n

        scaleTdistrn = (k_n + 1.) / (k_n * (nu_n - self.embedding_size + 1.))
        scaled_sigma_n = self.prior.sigma + self.sum_squared_table_customers[table_id] - mu_n_mu_nT + self.k0mu0mu0T
        sigma_n = scaled_sigma_n * scaleTdistrn
        # calculate det(Sigma)
        # Use slogdet to avoid under/overflow problems
        sign_det_sig, log_det_sig = slogdet(sigma_n)
        # The sign should always be 1, otherwise we'll run into problems when computing the likelihood
        if sign_det_sig != 1:
            self.log.warn("Error computing determinant of: {}. Table count = {}".format(sigma_n, count))
            raise ValueError("sign of log determinant of sigma is {}".format(sign_det_sig))
        # Now calculate Sigma^(-1) and det(Sigma) and store them
        # calculate Sigma^(-1)
        inv_sigma_n = inv(sigma_n)
        return mu_n, sigma_n, inv_sigma_n, log_det_sig, scaled_sigma_n

   

    def update_table_params_chol(self, table_id, cust_id, is_removed=False):
        count = self.table_counts[table_id]
        k_n = self.prior.kappa + count
        nu_n = self.prior.nu + count
        scaleTdistrn = (k_n + 1.) / (k_n * (float(nu_n) - self.embedding_size + 1.))

        if is_removed:
            # Now use the rank1 downdate to calculate the cholesky decomposition of the updated covariance matrix
            # The update equation is
            #   \Sigma_(N+1) =\Sigma_(N) - (k_0 + N+1) / (k_0 + N)(X_{n} - \mu_{n-1})(X_{n} - \mu_{n-1}) ^ T
            # Therefore x = sqrt((k_0 + N - 1) / (k_0 + N)) (X_{n} - \mu_{n})
            # Note here \mu_n will be the mean before updating.
            # After updating sigma_n, we will update \mu_n.

            # calculate (X_{n} - \mu_{n-1})
            # This uses the old mean, not yet updated
            x = (self.vocab_embeddings[cust_id] - self.table_means[table_id]) * np.sqrt((k_n + 1.) / k_n)
            # The Chol rank1 downdate modifies the array in place
            chol_rank1_downdate(self.table_cholesky_ltriangular_mat[table_id], x)

            # Update the mean
            new_mean = self.table_means[table_id] * (k_n + 1.)
            new_mean -= self.vocab_embeddings[cust_id]
            new_mean /= k_n
            self.table_means[table_id] = new_mean
        else:
            # New customer is added
            new_mean = self.table_means[table_id] * (k_n - 1.)
            new_mean += self.vocab_embeddings[cust_id]
            new_mean /= k_n
            self.table_means[table_id] = new_mean

            # We need to recompute det(Sig) and (v_{d,i} - mu) . Sig^-1 . (v_{d,i} - mu)
            # v_{d,i} is the word vector being added

            # The rank1 update equation is
            #  \Sigma_{n+1} = \Sigma_{n} + (k_0 + n + 1) / (k_0 + n) * (x_{n+1} - \mu_{n+1})(x_{n+1} - \mu_{n+1}) ^ T
            # calculate (X_{n} - \mu_{n-1})
            # This time we update the mean first and use the new mean
            x = (self.vocab_embeddings[cust_id] - self.table_means[table_id]) * np.sqrt(k_n / (k_n - 1.))
            # The update modifies the decomp array in place
            chol_rank1_update(self.table_cholesky_ltriangular_mat[table_id], x)

        # Calculate the 0.5 * log(det) + D / 2 * scaleTdistrn
        # The scaleTdistrn is because the posterior predictive distribution sends in a scaled value of \Sigma
        self.log_determinants[table_id] = \
            np.sum(np.log(np.diagonal(self.table_cholesky_ltriangular_mat[table_id]))) \
            + self.embedding_size * np.log(scaleTdistrn) / 2.

    

    
    def _log_multivariate_tdensity_chol(self, x, table_id):
        """
        Gaussian likelihood for a table-embedding pair when using Cholesky decomposition.

        """
        if x.ndim > 1:
            logprobs = np.zeros(x.shape[0], dtype=np.float64)
            for i in range(x.shape[0]):
                logprobs[i] = self._log_multivariate_tdensity_chol(x[i], table_id)
            return logprobs

        count = self.table_counts[table_id]
        k_n = self.prior.kappa + count
        nu_n = self.prior.nu + count
        scaleTdistrn = np.sqrt((k_n + 1.) / (k_n * (nu_n - self.embedding_size + 1.)))
        nu = self.prior.nu + count - self.embedding_size + 1.
        # Since I am storing lower triangular matrices, it is easy to calculate (x-\mu)^T\Sigma^-1(x-\mu)
        # therefore I am gonna use triangular solver
        # first calculate (x-mu)
        x_minus_mu = x - self.table_means[table_id]
        # Now scale the lower tringular matrix
        ltriangular_chol = scaleTdistrn * self.table_cholesky_ltriangular_mat[table_id]
        solved = solve_triangular(ltriangular_chol, x_minus_mu)
        # Now take xTx (dot product)
        val = (solved ** 2.).sum(-1)

        logprob = gammaln((nu + self.embedding_size) / 2.) - \
                  (
                          gammaln(nu / 2.) +
                          self.embedding_size / 2. * (np.log(nu) + np.log(math.pi)) +
                          self.log_determinants[table_id] +
                          (nu + self.embedding_size) / 2. * np.log(1. + val / nu)
                  )
        return logprob
    def log_multivariate_tdensity(self, x, table_id):
        """
        Density for a single table.

        Permits batching rows of x to compute density for multiple embeddings at once.

        This is for the non-Cholesky mode.

        """
        return self._log_multivariate_tdensity_chol(x, table_id)
        """
                mu = self.table_means[table_id]
                sigma_inv = self.table_inverse_covariances[table_id]
                count = self.table_counts[table_id]
                log_detr = self.log_determinants[table_id]

                # Now calculate the likelihood
                # calculate degrees of freedom of the T-distribution
                nu = self.prior.nu + count - self.embedding_size + 1.
                x_minus_mu = x - mu
                # Calculate (x = mu)^TSigma^(-1)(x = mu)
                # vec . mat -> vec
                prod = np.dot(x_minus_mu, sigma_inv)
                # vec . vec -> scalar
                # This is just a dot product, but implemented this way to allow us to batch x
                prod1 = np.sum(prod * x_minus_mu, -1)
                # Should be Nx1
                logprob = gammaln((nu + self.embedding_size) / 2.) - (
                            gammaln(nu / 2.) + self.embedding_size / 2. *
                            (np.log(nu) + np.log(math.pi)) + 0.5 * log_detr + (
                                nu + self.embedding_size) / 2. * np.log(1. + prod1 / nu))
                return logprob
        """
    
    def _log_multivariate_tdensity_chol_tables_text(self, x): 
        """
        Gaussian likelihood for a table-embedding pair when using Cholesky decomposition.
        This version computes the likelihood for all tables in parallel.

        """
        count = self.table_counts
        k_n = self.prior.kappa + count
        nu_n = self.prior.nu + count
        scaleTdistrn = np.sqrt((k_n + 1.) / (k_n * (nu_n - self.embedding_size + 1.)))
        nu = self.prior.nu + count - self.embedding_size + 1.
        # Since I am storing lower triangular matrices, it is easy to calculate (x-\mu)^T\Sigma^-1(x-\mu)
        # therefore I am gonna use triangular solver first calculate (x-mu)
        x_minus_mu = x[None, :] - self.table_means
        # Now scale the lower tringular matrix
        ltriangular_chol = scaleTdistrn[:, None, None] * self.table_cholesky_ltriangular_mat
        # We can't do solve_triangular for all matrices at once in scipy
        val = np.zeros(self.num_tables, dtype=np.float64)
        for table in range(self.num_tables):
            table_solved = solve_triangular(ltriangular_chol[table], x_minus_mu[table])
            # Now take xTx (dot product)
            val[table] = (table_solved ** 2.).sum()

        logprob = gammaln((nu + self.embedding_size) / 2.) - \
                (
                        gammaln(nu / 2.) +
                        self.embedding_size / 2. * (np.log(nu) + np.log(math.pi)) +
                        self.log_determinants +
                        (nu + self.embedding_size) / 2. * np.log(1. + val / nu)
                )
        return logprob
    
    def _log_multivariate_tdensity_chol_tables_audio(self, x):
        """
        Gaussian likelihood for a table-embedding pair when using Cholesky decomposition.
        This version computes the likelihood for all tables in parallel.

        """
        count = self.table_counts_audio
        k_n = self.prior_audio.kappa + count
        nu_n = self.prior_audio.nu + count
        scaleTdistrn = np.sqrt((k_n + 1.) / (k_n * (nu_n - self.audio_feature_size + 1.)))
        nu = self.prior_audio.nu + count - self.audio_feature_size + 1.
        # Since I am storing lower triangular matrices, it is easy to calculate (x-\mu)^T\Sigma^-1(x-\mu)
        # therefore I am gonna use triangular solver first calculate (x-mu)
        x_minus_mu = x[None, :] - self.table_means_audio
        # Now scale the lower tringular matrix
        ltriangular_chol = scaleTdistrn[:, None, None] * self.table_cholesky_ltriangular_mat_audio
        # We can't do solve_triangular for all matrices at once in scipy
        val = np.zeros(self.num_tables, dtype=np.float64)
        for table in range(self.num_tables):
            table_solved = solve_triangular(ltriangular_chol[table], x_minus_mu[table])
            # Now take xTx (dot product)
            val[table] = (table_solved ** 2.).sum()

        logprob = gammaln((nu + self.audio_feature_size) / 2.) - \
                  (
                          gammaln(nu / 2.) +
                          self.audio_feature_size / 2. * (np.log(nu) + np.log(math.pi)) +
                          self.log_determinants_audio +
                          (nu + self.audio_feature_size) / 2. * np.log(1. + val / nu)
                  )
        return logprob
    
    def log_multivariate_tdensity_tables(self, x, d_type=""):
        """
        Density for all tables in parallel. This version only allows a single x at a time,
        but all tables, as required for sampling.

        This is for the non-Cholesky mode.

        """
        if d_type == "audio":
            return self._log_multivariate_tdensity_chol_tables_audio(x)
        return self._log_multivariate_tdensity_chol_tables_text(x)
        """
                ## Do for table 0 for debugging
                # K x E
                mu = self.table_means
                #mu_0 = self.table_means[0]
                # K x E x E
                sigma_inv = self.table_inverse_covariances
                #sigma_inv_0 = self.table_inverse_covariances[0]
                # K
                count = self.table_counts
                #count_0 = self.table_counts[0]
                # K
                log_detr = self.log_determinants
                #log_detr_0 = self.log_determinants[0]

                # Now calculate the likelihood
                # calculate degrees of freedom of the T-distribution
                nu = self.prior.nu + count - self.embedding_size + 1.  # (K,)'
                #nu_0 = self.prior.nu + count_0 - self.embedding_size + 1.
                x_minus_mu = x[np.newaxis, :] - mu  # (K, E)
                #x_minus_mu_0 = x - mu_0
                # Calculate (x = mu)^TSigma^(-1)(x = mu)
                # vec . mat -> vec (batched)
                prod = np.sum(x_minus_mu[:, :, np.newaxis] * sigma_inv, axis=-1)  # (K, E)
                #prod_0 = np.dot(x_minus_mu_0, sigma_inv_0)
                # vec . vec -> scalar
                # This is just a dot product, but implemented this way to allow us to batch tables
                prod1 = np.sum(prod * x_minus_mu, -1)
                #prod1_0 = np.sum(prod_0 * x_minus_mu_0, -1)
                # (K,) -- one value per table
                logprob = gammaln((nu + self.embedding_size) / 2.) - (
                            gammaln(nu / 2.) + self.embedding_size / 2. *
                            (np.log(nu) + np.log(math.pi)) + 0.5 * log_detr + (
                                nu + self.embedding_size) / 2. * np.log(1. + prod1 / nu))
                return logprob
        """

    def rm_id_table_text(self,d,w,x,cust_id):
        
        # Remove custId from his old_table
        old_table_id = self.table_assignments[d][w]
        self.table_assignments[d][w] = -1  # Doesn't really make any difference, as only counts are used
        self.table_counts[old_table_id] -= 1
        self.table_counts_per_doc[old_table_id, d] -= 1
        # Update vector means etc
        self.sum_table_customers[old_table_id] -= x
        self.sum_squared_table_customers[old_table_id] -= np.outer(x, x)

        # Topic 'old_tabe_id' now has one member fewer
        # Just update params for this customer
        self.update_table_params_text(old_table_id, cust_id, is_removed=True)
        
    def rm_id_table_audio(self,d,w,x):
        
        # Remove custId from his old_table
        old_table_id = self.table_assignments_audio[d][w]
        self.table_assignments_audio[d][w] = -1  # Doesn't really make any difference, as only counts are used
        self.table_counts_audio[old_table_id] -= 1
        self.table_counts_per_doc_audio[old_table_id, d] -= 1
        if self.table_counts_per_doc_audio[old_table_id, d] < 0:
            self.table_counts_per_doc_audio[old_table_id, d]+=1
            print(self.table_counts_per_doc_audio[:, d] +1)
            print(self.table_counts_per_doc_audio[:, d])
        # Update vector means etc
        self.sum_table_customers_audio[old_table_id] -= x
        self.sum_squared_table_customers_audio[old_table_id] -= np.outer(x, x)

        # Topic 'old_tabe_id' now has one member fewer
        # Just update params for this customer
        self.update_table_params_audio(old_table_id, x, is_removed=True)
        
    def update_table_text(self,d,w,x,new_table_id,cust_id):
        self.table_assignments[d][w] = new_table_id
        self.table_counts[new_table_id] += 1
        self.table_counts_per_doc[new_table_id, d] += 1
        self.sum_table_customers[new_table_id] += x
        self.sum_squared_table_customers[new_table_id] += np.outer(x, x)
        self.update_table_params_text(new_table_id, cust_id)
        
    def update_table_audio(self,d,w,x,new_table_id):
        self.table_assignments_audio[d][w] = new_table_id
        self.table_counts_audio[new_table_id] += 1
        self.table_counts_per_doc_audio[new_table_id, d] += 1
        self.sum_table_customers_audio[new_table_id] += x
        self.sum_squared_table_customers_audio[new_table_id] += np.outer(x, x)
        self.update_table_params_audio(new_table_id, x)
        
    def sample(self, num_iterations):
        """
        for num_iters:
            for each customer
                remove him from his old_table and update the table params.
                if old_table is empty:
                    remove table
                Calculate prior and likelihood for this customer sitting at each table
                sample for a table index
                if new_table is equal to old_table
                    don't have to update the parameters
                else update params of the old table.
        """
        for iteration in range(num_iterations):
            self.log.info("Iteration {}".format(iteration))
            
            pbar = get_progress_bar(len(self.corpus), title="Sampling")
            for d, doc in enumerate(pbar(self.corpus)):
                if self.show_topics is not None and self.show_topics > 0 and d % self.show_topics == 0:
                    print("Topics after {:,} docs".format(d))
                    print(self.format_topics())
                if  len(doc) == 0:
                    continue
                audio_doc = self.audio_corpus[d]
                frame_start = 0
                pad = len(audio_doc) // len(doc)

                for w, cust_id in enumerate(doc):
                    x = self.vocab_embeddings[cust_id]
                    self.rm_id_table_text(d,w,x,cust_id)
                    # Now calculate the prior and likelihood for the customer to sit in each table and sample
                    # Go over each table
                    counts_text = self.table_counts_per_doc[:, d] + self.alpha
                    # Now calculate the likelihood for each table
                    log_lls_text = self.log_multivariate_tdensity_tables(x)
                    
                    # Add log prior in the posterior vector
                    log_posteriors_text = np.log(counts_text) + log_lls_text
 
                    for frame_index in range(frame_start,pad):
                        y = audio_doc[frame_index]
                        self.rm_id_table_audio(d,frame_index,y)
                        counts_audio = self.table_counts_per_doc_audio[:, d] + self.alpha
                        log_lls_audio = self.log_multivariate_tdensity_tables(y,"audio")
                        log_posteriors_audio = np.log(counts_audio) + log_lls_audio
                        log_posteriors = log_posteriors_text + log_posteriors_audio
 
                        posterior = np.exp(log_posteriors - log_posteriors.max())
                        posterior /= posterior.sum()
                        # Now sample an index from this posterior vector.
                        new_table_id = np.random.choice(self.num_tables, p=posterior)
                        self.update_table_audio(d, frame_index, y,new_table_id)
                        
                    pad+=pad
                    frame_start=pad

                                    
                    log_posteriors = log_posteriors_text + log_posteriors_audio
                    # To prevent overflow, subtract by log(p_max).
                    # This is because when we will be normalizing after exponentiating,
                    # each entry will be exp(log p_i - log p_max )/\Sigma_i exp(log p_i - log p_max)
                    # the log p_max cancels put and prevents overflow in the exponentiating phase.
                    posterior = np.exp(log_posteriors - log_posteriors.max())
                    posterior /= posterior.sum()
                    # Now sample an index from this posterior vector.
                    new_table_id = np.random.choice(self.num_tables, p=posterior)

                    # Now have a new assignment: add its counts
                    self.update_table_text(d, w, x,new_table_id, cust_id)

                    #self.check_everything(iteration, d, w)
                    

                #if self.cholesky_decomp:
                #    # After each iteration, recompute the Cholesky decomposition fully, to avoid numerical inaccuracies
                #    # blowing up with the repeated updates
                #    # This also recomputes means
                #    for table in range(self.num_tables):
                #        inv_sigma = self.set_table_parameters(table)
                #        self.table_cholesky_ltriangular_mat[table] = cholesky(inv_sigma)

            if self.show_topics is not None:
                print("Topics after iteration {}".format(iteration))
                print(self.format_topics())

            if self.save_path is not None:
                self.log.info("Saving model")
                self.save()

    def format_topics(self, num_words=10, topics=None):
        if topics is None:
            topics = list(range(self.num_tables))

        if self.num_words_for_formatting is not None:
            # Limit to the first N words to consider for inclusion in a topic's representation
            embeddings = self.vocab_embeddings[:self.num_words_for_formatting]
        else:
            embeddings = self.vocab_embeddings

        topic_fmt = []
        for topic in topics:
            if self.table_counts[topic] == 0:
                # This topic is never used, so should be considered to have been abandoned by the sampler (for now)
                topic_fmt.append("{}: unused")
            else:
                # Compute the density for all words in the vocab
                word_scores = self.log_multivariate_tdensity(embeddings, topic)
                word_probs = np.exp(word_scores - word_scores.max())
                word_probs /= word_probs.sum()
                topic_fmt.append(
                    "{}: {}".format(
                        topic,
                        " ".join(
                            "{} ({:.2e})".format(self.vocab[word], word_probs[word])
                            for word in np.argsort(-word_scores)[:num_words]
                        )
                    )
                )

        return "\n".join(topic_fmt)

    def save(self):
        if os.path.exists(self.save_path):
            shutil.rmtree(self.save_path)
        os.makedirs(self.save_path)

        with open(os.path.join(self.save_path, "params.json"), "w") as f:
            json.dump({
                "alpha": self.alpha,
                "vocab": self.vocab,
                "num_tables": self.num_tables,
                "kappa": self.prior.kappa,
            }, f)
        for name, data in [
            ("table_counts", self.table_counts),
            ("table_means", self.table_means),
            ("table_inverse_covariances", self.table_inverse_covariances),
            ("log_determinants", self.log_determinants),
            ("sum_table_customers", self.sum_table_customers),
            ("sum_squared_table_customers", self.sum_squared_table_customers),
            ("table_cholesky_ltriangular_mat", self.table_cholesky_ltriangular_mat),
        ]:
            with open(os.path.join(self.save_path, "{}.pkl".format(name)), "wb") as f:
                pickle.dump(data, f)

    def check_everything(self, iteration=None, doc_num=None, word_num=None, mid_sample=False):
        """
        Compute counts from the table assignments and compare to cached values.
        Also computes means, covariances and so on to check that the iteratively
        updated values match.

        For debugging only!

        Checks (* = not implemented yet):
         - table_counts
         - table_counts_per_doc
         - table_means
         - *table_inverse_covariances
         - *log_determinants
         - sum_table_customers
         - sum_squared_table_customers
         - table_cholesky_ltriangular_mat

        """
        warnings.warn("Checking table counts in full: this should only be done for debugging purposes")
        if iteration is not None and doc_num is not None and word_num is not None:
            mess = "\nIteration {:,}: doc {:,}. word {}{}".format(
                iteration, doc_num, word_num, " (after unsampling)" if mid_sample else "")
        else:
            mess = ""

        # Count customers per table and check self.table_counts and self.table_counts_per_doc
        table_counts = np.zeros((self.num_tables), dtype=np.int32)
        for doc_num, doc in enumerate(self.table_assignments):
            # Exclude any -1s, which represent a removed sample
            doc = [table for table in doc if table != -1]
            table_counts_for_doc = np.bincount(doc, minlength=self.num_tables)
            if not np.all(table_counts_for_doc == self.table_counts_per_doc[:, doc_num]):
                raise ValueError("DEBUG: table counts don't match for doc {}: {} != {}".format(
                    doc_num, table_counts_for_doc, self.table_counts_per_doc[:, doc_num]))
            table_counts += table_counts_for_doc
        if not np.all(table_counts == self.table_counts):
            raise ValueError("table counts for each doc correct, but overall counts wrong: {} != {}{}".format(
                table_counts, self.table_counts, mess
            ))

        # Collect the IDs of customers at each table
        table_ids = [[] for i in range(self.num_tables)]
        for doc, doc_tables in zip(self.corpus, self.table_assignments):
            for word, table in zip(doc, doc_tables):
                # Skip any value that's been unsampled
                if table != -1:
                    table_ids[table].append(word)

        # Compute mean and covariance for each table and compare to stored values
        for table, customers in enumerate(table_ids):
            # Check the simple sums of table customers and squared customers
            sum_customers = np.sum(self.vocab_embeddings[customers], axis=0)
            # Compare to the stored sum
            diff = np.mean(np.abs(sum_customers - self.sum_table_customers[table]))
            if diff > 1e-3:
                raise ValueError("Stored and computed vector sums for table {} don't match:\n{}\n{}\nDiff per dim: {}{}".format(
                    table, sum_customers, self.sum_table_customers[table], diff, mess
                ))
            # And for the squares
            sum_squared_customers = np.zeros((self.embedding_size, self.embedding_size), dtype=np.float64)
            for cust in customers:
                sum_squared_customers += np.outer(self.vocab_embeddings[cust], self.vocab_embeddings[cust])
            diff = np.mean(np.abs(sum_squared_customers - self.sum_squared_table_customers[table]))
            if diff > 1e-3:
                raise ValueError("Stored and computed vector square sums for table {} don't match:\n{}\n{}\nDiff per dim: {}{}".format(
                    table, sum_squared_customers, self.sum_squared_table_customers[table], diff, mess
                ))

            # Compute table means, including prior
            table_mean = (sum_customers + self.prior.mu*self.prior.kappa) / (len(customers) + self.prior.kappa)
            # Compare to the stored mean
            diff = np.mean(np.abs(table_mean - self.table_means[table]))
            if diff > 1e-3:
                raise ValueError("Stored and computed means for table {} don't match:\n{}\n{}\nDiff per dim: {}{}".format(
                    table, table_mean, self.table_means[table], diff, mess
                ))

            if self.cholesky_decomp:
                # Check that the Cholesky decomposition matches the actual covariance matrix
                count = self.table_counts[table]
                k_n = self.prior.kappa + count
                nu_n = self.prior.nu + count
                mu_n_mu_nT = np.outer(table_mean, table_mean) * k_n
                # The following scaling factor gives the full covariance matrix,
                # but the cholesky decomposition is of the matrix without this factor
                #scaleTdistrn = (k_n + 1.) / (k_n * (nu_n - self.embedding_size + 1.))
                # Compute the covariance matrix in full
                sigma_n = (self.prior.sigma + self.sum_squared_table_customers[table] - mu_n_mu_nT + self.k0mu0mu0T)
                # We perform the Cholesky decomposition of the fully computed sigma and then
                # compare this to the iteratively updated decomposition matrix
                sigma_chol = cholesky(sigma_n)
                updated_chol = self.table_cholesky_ltriangular_mat[table]
                diff = np.mean(np.abs(updated_chol - sigma_chol))
                if diff > 1e-3:
                    raise ValueError("Cholesky decomposition of cov mat and iteratively updated "
                                     "Cholesky decomposition mat for "
                                     "table {} don't match:\n{}\n{}\nDiff per dim: {}{}".format(
                        table, updated_chol, sigma_chol, diff, mess
                    ))

