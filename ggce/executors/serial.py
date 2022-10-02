import numpy as np
from scipy import linalg
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import spsolve
import time

from ggce.executors.base import BaseExecutor
from ggce.engine.physics import G0_k_omega
from ggce.logger import logger, disable_logger


BYTES_TO_MB = 1048576
BYTES_TO_GB = 1073741274


class SerialSparseExecutor(BaseExecutor):
    """Uses the SciPy sparse solver engine to solve for G(k, w) in serial."""

    def _sparse_prime_helper(self):
        """Helper primer for use in the sparse solvers."""

        self._prime_system()
        self._basis = self._system.get_basis(full_basis=True)
        self._total_jobs_on_this_rank = 1

    def prime(self):
        self._sparse_prime_helper()

    def _sparse_matrix_from_equations(self, k, w, eta):
        """This function iterates through the GGCE equations dicts to extract
        the row, column coordiante and value of the nonzero entries in the
        matrix. This is subsequently used to construct the parallel sparse
        system matrix. This is exactly the same as in the Serial class: however
        that method returns X, v whereas here we need row_ind/col_ind_dat.

        Parameters
        ----------
        k : float
            The momentum quantum number point of the calculation.
        w : float
            The frequency grid point of the calculation.
        eta : float
            The artificial broadening parameter of the calculation.

        Returns
        -------
        list, list, list
            The row and column coordinate lists, as well as a list of values of
            the matrix that are nonzero.
        """

        t0 = time.time()

        row_ind = []
        col_ind = []
        dat = []

        total_bosons = np.sum(self._model.N)
        for n_bosons in range(total_bosons + 1):
            for eq in self._system.equations[n_bosons]:
                row_dict = dict()
                index_term_id = eq.index_term.identifier()
                ii_basis = self._basis[index_term_id]

                for term in eq.terms_list + [eq.index_term]:
                    jj = self._basis[term.identifier()]
                    try:
                        row_dict[jj] += term.coefficient(k, w, eta)
                    except KeyError:
                        row_dict[jj] = term.coefficient(k, w, eta)

                row_ind.extend([ii_basis for _ in range(len(row_dict))])
                col_ind.extend([key for key, _ in row_dict.items()])
                dat.extend([value for _, value in row_dict.items()])

        dt = time.time() - t0
        logger.debug("Sparse matrix initialized", elapsed=dt)

        # estimate sparse matrix memory usage
        # (complex (16 bytes) + int (4 bytes) + int) * nonzero entries
        est_mem_used = 24 * len(dat) / BYTES_TO_MB
        logger.debug(
            f"Estimated memory needed is {est_mem_used:.02f} MB"
        )

        return row_ind, col_ind, dat

    def _scaffold(self, k, w, eta):
        """Prepare the X, v sparse representation of the matrix to solve.

        Parameters
        ----------
        k : float
            The momentum quantum number point of the calculation.
        w : float
            The frequency grid point of the calculation.
        eta : float, optional
            The artificial broadening parameter of the calculation.

        Returns
        -------
        csr_matrix, csr_matrix
            Sparse representation of the matrix equation to solve, X and v.
        """

        t0 = time.time()

        row_ind, col_ind, dat = self._sparse_matrix_from_equations(k, w, eta)

        X = coo_matrix((
            np.array(dat, dtype=np.complex64),
            (np.array(row_ind), np.array(col_ind))
        )).tocsr()

        size = (X.data.size + X.indptr.size + X.indices.size) / BYTES_TO_MB

        logger.debug(f"Memory usage of sparse X is {size:.01f} MB")

        # Initialize the corresponding sparse vector
        # {G}(0)
        row_ind = np.array([self._basis['{G}(0.0)']])
        col_ind = np.array([0])
        v = coo_matrix((
            np.array(
                [self._system.equations[0][0].bias(k, w, eta)],
                dtype=np.complex64
            ), (row_ind, col_ind)
        )).tocsr()

        dt = time.time() - t0

        logger.debug("Scaffold complete", elapsed=dt)

        return X, v

    def solve(self, k, w, eta, index=None, **kwargs):
        """Solve the sparse-represented system.

        Parameters
        ----------
        k : float
            The momentum quantum number point of the calculation.
        w : float
            The frequency grid point of the calculation.
        eta : float
            The artificial broadening parameter of the calculation.
        index : int, optional
            The calculation index (the default is None).

        Returns
        -------
        np.ndarray, dict
            The value of G and meta information, which in this case, is only
            the time elapsed to solve for this (k, w) point.
        """

        t0 = time.time()
        X, v = self._scaffold(k, w, eta)

        # Bottleneck: solve the matrix
        res = spsolve(X, v)
        dt = time.time() - t0

        G = res[self._basis['{G}(0.0)']]
        A = -G.imag / np.pi

        if A < 0.0:
            self._log_spectral_error(k, w)

        self._log_current_status(k, w, A, index, dt)

        return np.array(G), {'time': [dt]}


class SerialDenseExecutor(BaseExecutor):
    """Uses the SciPy dense solver engine to solve for G(k, w) in serial. This
    method uses the continued fraction approach,

    .. math:: R_{n-1} = (1 - \\beta_{n-1}R_{n})^{-1} \\alpha_{n-1}

    with

    .. math:: R_n = \\alpha_n

    and

    .. math:: f_n = R_n f_{n-1}
    """

    def _dense_prime_helper(self):
        self._prime_system()
        self._basis = self._system.get_basis(full_basis=False)

    def prime(self):
        self._dense_prime_helper()

    def _fill_matrix(self, k, w, n_phonons, shift, eta):

        n_phonons_shift = n_phonons + shift

        equations_n = self._system.equations[n_phonons]

        # Initialize a matrix to fill
        d1 = len(self._basis[n_phonons])
        d2 = len(self._basis[n_phonons + shift])
        A = np.zeros((d1, d2), dtype=np.complex64)

        # Fill the matrix of coefficients
        for ii, eq in enumerate(equations_n):
            index_term_id = eq.index_term.identifier()
            ii_basis = self._basis[n_phonons][index_term_id]
            for term in eq.terms_list:
                if term.get_total_bosons() != n_phonons_shift:
                    continue
                t_id = term.identifier()
                jj_basis = self._basis[n_phonons_shift][t_id]
                A[ii_basis, jj_basis] += term.coefficient(k, w, eta)

        return A

    def _get_alpha(self, k, w, n_phonons, eta):

        t0 = time.time()
        A = self._fill_matrix(k, w, n_phonons, -1, eta)
        dt = time.time() - t0
        logger.debug("Filled alpha", elapsed=dt)
        return A

    def _get_beta(self, k, w, n_phonons, eta):

        t0 = time.time()
        A = self._fill_matrix(k, w, n_phonons, 1, eta)
        dt = time.time() - t0
        logger.debug("Filled beta", elapsed=dt)
        return A

    def solve(self, k, w, eta, index=None, **kwargs):
        """Solve the dense-represented system.

        Parameters
        ----------
        k : float
            The momentum quantum number point of the calculation.
        w : float
            The frequency grid point of the calculation.
        eta : float
            The artificial broadening parameter of the calculation.
        index : int, optional
            The calculation index (the default is None).

        Returns
        -------
        np.ndarray, dict
            The value of G and meta information, which in this case, is only
            the time elapsed to solve for this (k, w) point.
        """

        t0_all = time.time()

        meta = {
            'alphas': [],
            'betas': [],
            'inv': [],
            'time': []
        }

        finfo = self._model.get_fFunctionInfo()

        total_phonons = np.sum(self._model.N)

        for n_phonons in range(total_phonons, 0, -1):

            # Special case of the recursion where R_N = alpha_N.
            if n_phonons == total_phonons:
                R = self._get_alpha(k, w, n_phonons, eta)
                meta["alphas"].append(R.shape)
                continue

            # Get the next loop's alpha and beta values
            beta = self._get_beta(k, w, n_phonons, eta)
            meta["betas"].append(beta.shape)
            alpha = self._get_alpha(k, w, n_phonons, eta)
            meta["alphas"].append(alpha.shape)

            # Compute the next R
            X = np.eye(beta.shape[0], R.shape[1]) - beta @ R
            meta["inv"].append(X.shape[0])
            t0 = time.time()
            R = linalg.solve(X, alpha)
            dt = time.time() - t0
            logger.debug(
                f"Inverted [{X.shape}, {alpha.shape}]", elapsed=dt
            )
            meta["time"].append(dt)

        G0 = G0_k_omega(k, w, finfo.a, eta, finfo.t)

        beta0 = self._get_beta(k, w, 0, eta)
        G = (G0 / (1.0 - beta0 @ R)).squeeze()

        dt_all = time.time() - t0_all

        meta["time"].append(dt_all)

        A = -G.imag / np.pi

        if A < 0.0:
            self._log_spectral_error(k, w)

        self._log_current_status(k, w, A, index, dt_all)

        return np.array(G, dtype=np.complex64), meta
