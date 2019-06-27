"""Defines PLDCorrector.

This module requires 3 optional dependencies (theano, pymc3, exoplanet) for
PLD correction to work.  One additional dependency (fbpca) is not required
but will speed up the computation if installed.

TODO
----
* Make sure the prior of logs2/logsigma/logrho has a user-configurable width.
* [Done] Verify the units of corrected_flux, corrected_flux_without_gp, etc.
  In Lightkurve we currently add back the mean of the model.
* The design matrix can be improved by rejecting pixels which are saturated,
  and including the collapsed sums of their CCD columns instead.
* Set PyMC verbosity to match the lightkurve.log.level.
* It is not clear whether the inclusion of a column vector of ones in the
  design matrix is necessary for numerical stability.
"""
import logging
import warnings
from itertools import combinations_with_replacement as multichoose

import numpy as np
import matplotlib.pyplot as plt

# Optional dependencies
try:
    import pymc3 as pm
    import exoplanet as xo
    import theano.tensor as tt
except ImportError:
    # Fail quietly here so we don't break `import lightkurve`.
    # We will raise a user-friendly ImportError inside PLDCorrector.__init__().
    pass

from .. import MPLSTYLE
from ..utils import LightkurveError, LightkurveWarning, suppress_stdout


from astropy.modeling.models import Gaussian1D, Box1D

log = logging.getLogger(__name__)

__all__ = ['PLDCorrector']


class PLDCorrector(object):
    r"""Implements the Pixel Level Decorrelation (PLD) systematics removal method.

        Pixel Level Decorrelation (PLD) was developed by [1]_ to remove
        systematic noise caused by spacecraft jitter for the Spitzer
        Space Telescope. It was adapted to K2 data by [2]_ and [3]_
        for the EVEREST pipeline [4]_.

        For a detailed description and implementation of PLD, please refer to
        these references. Lightkurve provides a reference implementation
        of PLD that is less sophisticated than EVEREST, but is suitable
        for quick-look analyses and detrending experiments.

        Background
        ----------
        Our implementation of PLD is performed by first calculating the noise
        model for each cadence in time. This function goes up to arbitrary
        order, and is represented by

        .. math::

            m_i = \sum_l a_l \frac{f_{il}}{\sum_k f_{ik}} + \sum_l \sum_m b_{lm} \frac{f_{il}f_{im}}{\left( \sum_k f_{ik} \right)^2} + ...
        where

          - :math:`m_i` is the noise model at time :math:`t_i`
          - :math:`f_{il}` is the flux in the :math:`l^\text{th}` pixel at time :math:`t_i`
          - :math:`a_l` is the first-order PLD coefficient on the linear term
          - :math:`b_{lm}` is the second-order PLD coefficient on the :math:`l^\text{th}`,
            :math:`m^\text{th}` pixel pair

        We perform Principal Component Analysis (PCA) to reduce the number of
        vectors in our final model to limit the set to best capture instrumental
        noise. With a PCA-reduced set of vectors, we can construct a design matrix
        containing fractional pixel fluxes.

        To capture long-term variability, we simultaneously fit a Gaussian Process
        model ([5]_) to the underlying stellar signal. We use the gradient-based
        probabilistic modeling toolkit [6]_ to optimize the GP hyperparameters and
        solve for the motion model.

        To robustly estimate errors on our parameter estimates and output flux
        values, we optionally sample the output model with [7]_ and infer errors from
        the posterior distribution.

        To solve for the PLD model, we need to minimize the difference squared

        .. math::

            \chi^2 = \sum_i \frac{(y_i - m_i)^2}{\sigma_i^2},

        where :math:`y_i` is the observed flux value at time :math:`t_i`, by solving

        .. math::

            \frac{\partial \chi^2}{\partial a_l} = 0.

    Examples
    --------
    Download the pixel data for GJ 9827 and obtain a PLD-corrected light curve:

    >>> import lightkurve as lk
    >>> tpf = lk.search_targetpixelfile("GJ9827").download() # doctest: +SKIP
    >>> corrector = lk.PLDCorrector(tpf) # doctest: +SKIP
    >>> lc = corrector.correct() # doctest: +SKIP
    >>> lc.plot() # doctest: +SKIP

    However, the above example will over-fit the small transits!
    It is necessary to mask the transits using `corrector.correct(cadence_mask=...)`.

    References
    ----------
    .. [1] Deming et al. (2015), ads:2015ApJ...805..132D.
        (arXiv:1411.7404)
    .. [2] Luger et al. (2016), ads:2016AJ....152..100L
        (arXiv:1607.00524)
    .. [3] Luger et al. (2018), ads:2018AJ....156...99L
        (arXiv:1702.05488)
    .. [4] EVEREST pipeline webpage, https://rodluger.github.io/everest
    .. [5] Celerite documentation, https://celerite.readthedocs.io/en/stable/
    .. [6] Exoplanet documentation, https://exoplanet.readthedocs.io/en/stable/
    .. [7] PyMC3 documentation, https://docs.pymc.io

    Parameters
    ----------
    tpf : `TargetPixelFile` object
        The pixel data from which a light curve will be extracted.
    aperture_mask : 2D boolean array or str
        The pixel aperture mask that will be used to extract the raw light curve.
    design_matrix_aperture_mask : 2D boolean array or str
        The pixel aperture mask that will be used to create the regression matrix
        (i.e. the design matrix) used to model the systematics.  If `None`,
        then the `aperture_mask` value will be used.

    Raises
    ------
    ImportError : if one of Lightkurve's optional dependencies required by
        this class are not available (i.e. `pymc`, `theano`, or `exoplanet`).
    """
    def __init__(self, tpf, aperture_mask=None, design_matrix_aperture_mask='all'):
        # Ensure the optional dependencies requires by this class are installed
        success, messages = self._check_optional_dependencies()
        if not success:
            for message in messages:
                log.error(message)
            raise ImportError("\n".join(messages))

        # C: We should use the pipeline mask by default. `to_lightcurve` defaults to the pipeline mask. Otherwise, when I plot side by side there is an offset.
        if aperture_mask is None:
            aperture_mask = tpf.pipeline_mask

        # Input validation: parse the aperture masks to accept strings etc.
        self.aperture_mask = tpf._parse_aperture_mask(aperture_mask)
        self.design_matrix_aperture_mask = tpf._parse_aperture_mask(design_matrix_aperture_mask)
        # Generate raw flux light curve from desired pixels
        raw_lc = tpf.to_lightcurve(aperture_mask=self.aperture_mask)
        # It is critical to remove all cadences with NaNs or the linear algebra below will crash
        self.raw_lc, self.nan_mask = raw_lc.remove_nans(return_mask=True)
        self.tpf = tpf[~self.nan_mask]

        # Theano raises an `AsTensorError` ("Cannot convert to TensorType")
        # if the floats are not in 64-bit format, so we convert them here:
        self._lc_time = np.asarray(self.raw_lc.time, np.float64)
        self._lc_flux = np.asarray(self.raw_lc.flux, np.float64)
        self._lc_flux_err = np.asarray(self.raw_lc.flux_err, np.float64)

        self._s2_mu = np.var(self._lc_flux.flatten())

        # For user-friendliness, we will track the most recently used model and solution:
        self.most_recent_model = None
        self.most_recent_solution = None
        self.most_recent_trace = None

    def _check_optional_dependencies(self):
        """Emits a user-friendly error message if one of Lightkurve's
        optional dependencies which are required for PLDCorrector are missing.

        Returns
        -------
        success : bool
            True if all optional dependencies are available, False otherwise.
        message : list of str
            User-friendly error message if success == False.
        """
        success, messages = True, []

        try:
            import pymc3 as pm
        except ImportError:
            success = False
            messages.append("PLDCorrector requires pymc3 to be installed (`pip install pymc3`).")

        try:
            import exoplanet as xo
        except ImportError:
            success = False
            messages.append("PLDCorrector requires exoplanet to be installed (`pip install exoplanet`).")

        try:
            import theano.tensor as tt
        except ImportError:
            success = False
            messages.append("PLDCorrector requires theano to be installed (`pip install theano`).")

        return success, messages

    def _create_first_order_matrix(self, normalize=True):
        """Returns a matrix which encodes the fractional pixel fluxes as a function
        of cadence (row) and pixel (column). As such, the method returns a
        2D matrix with shape (n_cadences, n_pixels_in_pld_mask).

        This matrix will form the basis of the PLD regressor design matrix
        and is often called the first order component. The matrix returned
        here is guaranteed to be free of NaN values.

        Returns
        -------
        matrix : numpy array
            First order PLD design matrix.
        """
        # Re-arrange the cube of flux values observed in a user-specified mask
        # into a 2D matrix of shape (n_cadences, n_pixels_in_mask).
        # Note that Theano appears to require 64-bit floats.
        matrix = np.asarray(self.tpf.flux[:, self.design_matrix_aperture_mask], np.float64)
        assert matrix.shape == (len(self.raw_lc.time), self.design_matrix_aperture_mask.sum())
        # Remove all NaN or Inf values
        matrix = matrix[:, np.isfinite(matrix).all(axis=0)]
        # To ensure that each column contains the fractional pixel flux,
        # we divide by the sum of all pixels in the same cadence.
        # This is an important step, as explained in Section 2 of Luger et al. (2016).
        if normalize:
            matrix = matrix / np.sum(matrix, axis=-1)[:, None]
        # If we return matrix at this point, theano will raise a "dimension mismatch".
        # The origin of this bug is not understood, but copying the matrix
        # into a new one as shown below circumvents it:
        result = np.empty((matrix.shape[0], matrix.shape[1]))
        result[:, :] = matrix[:, :]

        return result

    def create_design_matrix(self, pld_order=1, n_pca_terms=20, **kwargs):
        """Returns a matrix designed to contain suitable regressors for the
        systematics noise model.

        The design matrix contains one row for each cadence (i.e. moment in time)
        and one column for each regressor that we wish to use to predict the
        systematic noise in a given cadence.

        The columns (i.e. regressors) included in the design matrix are:
        * One column for each pixel in the PLD aperture mask.  Each column
          contains the flux values observed by that pixel over time.  This is
          also known as the first order component.
        * Columns derived from the products of all combinations of pixel values
          in the aperture mask. However, rather than including a column for each
          combination, we perform dimensionality reduction (PCA) and include a
          smaller number of PCA terms, i.e. the number of columns is
          n_pca_terms*(pld_order-1).  This is also known as the higher order
          components.

        Thus, the shape of the design matrix will be
        (n_cadences, n_pld_mask_pixels + n_pca_terms*(pld_order-1))

        Parameters
        ----------
        pld_order : int
            The order of Pixel Level De-correlation to be performed. First order
            (`n=1`) uses only the pixel fluxes to construct the design matrix.
            Higher order populates the design matrix with columns constructed
            from the products of pixel fluxes.
        n_pca_terms : int
            Number of terms added to the design matrix from each order of PLD
            when performing Principal Component Analysis for models higher than
            first order. Increasing this value may provide higher precision at
            the expense of computational time.

        Returns
        -------
        design_matrix : 2D numpy array
            See description above.
        """
        # We use an optional dependency for very fast PCA (fbpca), but if the
        # import fails we will fall back on using the slower `np.linalg.svd`.
        use_fbpca = True
        try:
            from fbpca import pca
        except ImportError:
            use_fbpca = False
            log.warning("PLD systematics correction will run faster if the "
                        "optional `fbpca` package is installed "
                        "(`pip install fbpca`).")

        matrix_sections = []  # list to hold the design matrix components
        first_order_matrix = self._create_first_order_matrix()

        # Input validation: n_pca_terms cannot be larger than the number of regressors (pixels)
        n_pixels = len(first_order_matrix.T)
        if n_pca_terms > n_pixels:
            log.warning("`n_pca_terms` ({}) cannot be larger than the number of pixels ({});"
                        "using n_pca_terms={}".format(n_pca_terms, n_pixels, n_pixels))
            n_pca_terms = n_pixels

        # Get the normalization matrix
        norm = np.sum(self._create_first_order_matrix(normalize=False), axis=1)[:, None]

        # Add the higher order PLD design matrix columns
        for order in range(2, pld_order + 1):
            # Take the product of all combinations of pixels; order=2 will
            # multiply all pairs of pixels, order=3 will multiple triples, etc.
            matrix = np.product(list(multichoose(first_order_matrix.T, order)), axis=1).T
            # This product matrix becomes very big very quickly, so we reduce
            # its dimensionality using PCA.
            if use_fbpca:  # fast mode
                components, _, _ = pca(matrix, n_pca_terms)
            else:  # slow mode
                components, _, _ = np.linalg.svd(matrix)
            section = components[:, :n_pca_terms]
            # Normalize the higher order components
            section = section / norm**order
            matrix_sections.append(section)

        if use_fbpca:  # fast mode
            first_order_matrix, _, _ = pca(first_order_matrix, n_pca_terms)
        else:  # slow mode
            first_order_matrix, _, _ = np.linalg.svd(first_order_matrix)[:, :n_pca_terms]

        # If we return matrix at this point, theano will raise a "dimension mismatch".
        # The origin of this bug is not understood, but copying the matrix
        # into a new one as shown below circumvents it:
        result = np.empty((first_order_matrix.shape[0], first_order_matrix.shape[1]))
        result[:, :] = first_order_matrix[:, :]

        # Add the first order matrix
        matrix_sections.insert(0, first_order_matrix)
        design_matrix = np.concatenate(matrix_sections, axis=1)

        # No columns in the design matrix should be NaN
        assert np.isfinite(design_matrix).any()

        # This gotcha cropped up when running kepler-11
        dt = np.empty((design_matrix.shape[0], design_matrix.shape[1]))
        dt[:, :] = design_matrix[:, :]
        return dt

    def create_pymc_model(self, design_matrix, cadence_mask=None,
                          gp_timescale_prior=150, fractional_prior_width=.05, **kwargs):
        r"""Returns a PYMC3 model.

        Parameters
        ----------
        design_matrix : np.ndarray
            Matrix of shape (n_cadences, n_regressors) used to create the
            motion model.  If `None`, then the output of this object's
            `create_design_matrix` method will be used.
        cadence_mask : np.ndarray
            Boolean array to mask cadences. Cadences that are False will be excluded
            from the model fit.  If `None`, then all cadences will be used.
        gp_timescale_prior : int or float
            The parameter `rho` in the definition of the Matern-3/2 kernel, which
            influences the timescale of variability fit by the Gaussian Process,
            in the same units as `tpf.time`.
            For more information, see [1]

        Returns
        -------
        model : pymc3.model.Model
            A pymc3 model.

        References
        ----------
        .. [1] the `celerite` documentation https://celerite.readthedocs.io
        """
        if cadence_mask is None:
            cadence_mask = np.ones(len(self.raw_lc.time), dtype=bool)

        # Covariance matrix diagonal
        diag = self._lc_flux_err**2

        # The cadence mask is applied by inflating the uncertainties in the covariance matrix;
        # this is because celerite will run much faster if it is able to predict
        # data for cadences that have been fed into the model.
        diag[~cadence_mask] += 1e12

        with pm.Model() as model:
            # The mean baseline flux value for the star
            mean = pm.Normal("mean", mu=np.nanmean(self._lc_flux), sd=np.std(self._lc_flux))
            # Create a Gaussian Process to model the long-term stellar variability
            # log(sigma) is the amplitude of variability, estimated from the raw flux scatter
            # logsigma = pm.Normal("logsigma", mu=np.log(np.std(self._lc_flux)), sd=2)
            logsigma = pm.Uniform("logsigma", testval=np.log(np.std(self._lc_flux)),
                                  lower=np.log(np.std(self._lc_flux))-10,
                                  upper=np.log(np.std(self._lc_flux))+10)
            # log(rho) is the timescale of variability with a user-defined prior
            # Enforce that the scale of variability should be no shorter than 0.5 days
            # logrho = pm.Normal("logrho", mu=np.log(gp_timescale_prior),
            #                    sd=2)
            logrho = pm.Uniform("logrho", testval=np.log(gp_timescale_prior),
                                lower=np.log(0.5),
                                upper=np.log(gp_timescale_prior)+10)
            # log(s2) is a jitter term to compensate for underestimated flux errors
            # We estimate the magnitude of jitter from the CDPP (normalized to the flux)
            # logs2 = pm.Normal("logs2", mu=np.log(self._s2_mu), sd=2)
            logs2 = pm.Uniform("logs2", testval=np.log(self._s2_mu),
                               lower=np.log(self._s2_mu)-5,
                               upper=np.log(self._s2_mu)+5)
            # logs2 = pm.Constant("logs2", np.log(self._s2_mu))
            kernel = xo.gp.terms.Matern32Term(log_sigma=logsigma, log_rho=logrho)

            # Store the GP and cadence mask to aid debugging
            model.gp = xo.gp.GP(kernel, self._lc_time, diag + tt.exp(logs2))
            model.cadence_mask = cadence_mask

            # The motion model regresses against the design matrix
            A = tt.dot(design_matrix.T, model.gp.apply_inverse(design_matrix))
            # To ensure the weights can be solved for, we need to perform L2 normalization
            # to avoid an ill-conditioned matrix A. Here we define the size of the diagonal
            # along which we will add small values
            diag_inds = np.array(range(design_matrix.shape[1]))
            # Cast the diagonal indices into tensor space
            diag_inds = tt.cast(diag_inds, 'int64')
            # Add small numbers along the diagonal
            A = tt.set_subtensor(A[diag_inds, diag_inds], A[diag_inds, diag_inds] + 1e-8)

            B = tt.dot(design_matrix.T, model.gp.apply_inverse(self._lc_flux[:, None]))
            weights = tt.slinalg.solve(A, B)
            motion_model = pm.Deterministic("motion_model", tt.dot(design_matrix, weights)[:, 0])
            pm.Deterministic("weights", weights)

            # Likelihood to optimize
            pm.Potential("obs", model.gp.log_likelihood(self._lc_flux - (motion_model + mean)))

            # Track the corrected flux values
            pm.Deterministic("corrected_flux", self._lc_flux - motion_model)

        self.most_recent_model = model
        return model

    # @suppress_stdout
    def optimize(self, model=None, start=None, robust=False, **kwargs):
        """Returns the maximum likelihood solution.

        Parameters
        ----------
        model : `pymc3.model.Model` object
            A pymc3 model.  If `None`, the the output of this object's
            `create_pymc_model` method will be used.
        start : dict
            MAP Solution from exoplanet
        robust : bool
            If `True`, all parameters will be optimized separately before
            attempting to optimize all parameters together.  This will be
            significantly slower but increases the likelihood of success.
        **kwargs : dict
            Dictionary of arguments to be passed to
            `~lightkurve.correctors.PLDCorrector.create_pymc_model`.

        Returns
        -------
        solution : dict
            Maximum likelihood values.
        """
        if model is None:
            model = self.create_pymc_model(**kwargs)
        if start is None:
            start = model.test_point

        with model:
            # If a solution cannot be found, fail with an informative LightkurveError
            try:
                solution = xo.optimize(start=start, vars=[model.logrho, model.mean])
                # Optimizing parameters separately appears to make finding a solution more likely
                if robust:
                    solution = xo.optimize(start=solution, vars=[model.logrho, model.logsigma, model.mean])
                    solution = xo.optimize(start=solution, vars=[model.logrho, model.logsigma])#, model.logs2])
                solution = xo.optimize(start=start)  # Optimize all parameters
            except ValueError:
                raise LightkurveError('Unable to find a noise model solution for the given '
                                      'target pixel file. Try increasing the PLD order or '
                                      'changing the `design_matrix_aperture_mask`.')

        self.most_recent_solution = solution
        return solution

    def sample(self, model=None, start=None, draws=1000, chains=4, **kwargs):
        """Sample the systematics correction model.

        Parameters
        ----------
        model : `pymc3.model.Model`
            A pymc3 model.
        start : dict
            Initial parameter values to initiate the sampling. If `None`,
            the output of this object's `optimize()` method will be used.
        draws : int
            Number of MCMC samples.
        chains : int
            Number of MCMC chains.

        Returns
        -------
        trace : `~pymc3.backends.base.MultiTrace`
            Trace object containing parameters and their samples.
        """
        # Create the model
        if model is None:
            model = self.create_pymc_model()
        if start is None:
            start = self.optimize(model=model)

        # Initialize the sampler
        sampler = xo.PyMC3Sampler()
        with model:
            # Burn in the sampler
            sampler.tune(tune=np.max([int(draws*0.3), 150]),
                         start=start,
                         step_kwargs=dict(target_accept=0.9),
                         chains=chains)
        with model:
            # Sample the parameters
            trace = sampler.sample(draws=draws, chains=chains)

        self.most_recent_trace = trace
        return trace

    def correct(self, sample=False, remove_gp_trend=False, **kwargs):
        """Returns a systematics-corrected light curve.

        Parameters
        ----------
        sample : boolean
            `True` will sample the output of the optimization
            step and include robust errors on the output light curve.
        remove_gp_trend : boolean
            `True` will subtract the fit the long term GP signal from the
            returned flux light curve.
        **kwargs : dict
            Optional arguments to be passed to
            `~lightkurve.correctors.PLDCorrector.create_pymc_model`,
            `~lightkurve.correctors.PLDCorrector.optimize`, and
            `~lightkurve.correctors.PLDCorrector.sample`.
        design_matrix : np.ndarray
            Matrix of shape (n_cadences, n_regressors) used to create the
            motion model.  If `None`, then the output of this object's
            `create_design_matrix` method will be used.
        cadence_mask : np.ndarray
            Boolean array to mask cadences. Cadences that are False will be excluded
            from the model fit.  If `None`, then all cadences will be used.
        gp_timescale_prior : int or float
            The parameter `rho` in the definition of the Matern-3/2 kernel, which
            influences the timescale of variability fit by the Gaussian Process,
            in the same units as `tpf.time`.
            For more information, see [1]
        model : `pymc3.model.Model` object
            A pymc3 model.  If `None`, the the output of this object's
            `create_pymc_model` method will be used.
        robust : bool
            If `True`, all parameters will be optimized separately before
            attempting to optimize all parameters together.  This will be
            significantly slower but increases the likelihood of success.
        start : dict
            Initial parameter values to initiate the sampling. If `None`,
            the output of this object's `optimize()` method will be used.
        draws : int
            Number of MCMC samples.
        chains : int
            Number of MCMC chains.

        Returns
        -------
        corrected_lc : `~lightkurve.lightcurve.LightCurve`
            Systematics-corrected light curve.
        """
        # Provide warning for deprecated syntax
        if any(mask in kwargs for mask in ['aperture_mask', 'design_matrix_aperture_mask']):
            warnings.warn('`PLDCorrector` has been recently updated, and no longer '
                          'accepts `aperture_mask` or `design_matrix_aperture_mask` '
                          'in the `correct` function. Please pass these masks into '
                          'the `PLDCorrector` constructor.', LightkurveWarning)

        if 'design_matrix' not in kwargs:
            kwargs['design_matrix'] = self.create_design_matrix()
        if kwargs['design_matrix'] is None:
            kwargs['design_matrix'] = self.create_design_matrix()

        self.design_matrix = kwargs['design_matrix']

        # Instantiate a PyMC3 model
        model = self.create_pymc_model(**kwargs)

        # Optimize the model parameters
        solution_or_trace = self.optimize(model=model, **kwargs)

        if sample:
            # Sample the posterior
            solution_or_trace = self.sample(model=model, start=solution_or_trace, **kwargs)

        """if remove_gp_trend:
            lc = self._lightcurve_from_solution(solution_or_trace, variable='corrected_flux')
            gp = self._gp_from_solution(self.most_recent_solution)
            # If passed a trace, we should take the median value of the `mean` parameter, not the optimized solution..
            mean = self.most_recent_solution['mean']
            lc.flux = lc.flux - (gp.flux  - mean)
        else:"""
        lc = self._lightcurve_from_solution(solution_or_trace, variable='corrected_flux')

        return lc

    def _lightcurve_from_solution(self, solution_or_trace, variable='corrected_flux'):
        """Helper function to generate light curve objects from the maximum
        likelihood solution or the sample trace.

        Parameters
        ----------
        solution_or_trace : dict or `~pymc3.backends.base.MultiTrace`
            The output returned by the `optimize()` or `sample()` methods
            of this object.
        variable : str
            Key for determining which light curve to extract from the given
            solution or trace.

        Returns
        -------
        lc : `~lightkurve.LightCurve`
            Light curve object corresponding to the model variable.
        """
        lc = self.raw_lc.copy()
        # If the model was sampled, use the mean and std of the flux values.
        if isinstance(solution_or_trace, pm.backends.base.MultiTrace):
            lc.flux = np.nanmean(solution_or_trace[variable], axis=0)
            lc.flux_err = np.nanstd(solution_or_trace[variable], axis=0)
        else:  # Otherwise, use the maximum likelihood flux values.
            lc.flux = solution_or_trace[variable]
        if variable == 'corrected_flux':
            lc.label = 'PLD Corrected {}'.format(self.raw_lc.label)
        elif variable == 'motion_model':
            lc.label = 'Motion Model for {}'.format(self.raw_lc.label)
        return lc

    def _gp_from_solution(self, solution):
        """Helper function to generate a light curve for the initial Gaussian
        Process fit from the most recent model model.

        Parameters
        ----------
        solution : dict
            The output returned by the `sample()` method.

        Returns
        -------
        lc : `~lightkurve.LightCurve`
            Light curve object with the Gaussian Process trend.
        """
        lc = self.raw_lc.copy()
        # Get the GP stored in the most recent model
        gp = self.most_recent_model.gp
        # Exoplanet requires arrays to be in float64 format
        time = np.array(self.raw_lc.time, dtype=np.float64)
        # Evaluate the most recent model using the most recent parameters
        with self.most_recent_model:
            mu, var = xo.eval_in_model(gp.predict(time, return_var=True), solution)
        lc.flux = mu
        lc.flux_err = np.sqrt(var)
        lc.label = 'PLD Corrected {} GP Trend'.format(self.raw_lc.label)
        return lc

    def get_diagnostic_lightcurves(self, solution_or_trace=None):
        """Return useful diagnostic light curves.

        Parameters
        ----------
        solution_or_trace : dict or `pymc3.backends.base.MultiTrace`
            The output returned by this object's `optimize()` or `sample()`
            methods.  If `None`, then the solution most recently computed
            by those methods will be used.

        Returns
        -------
        corrected_lc : `~lightkurve.lightcurve.LightCurve`
            Motion noise corrected light curve object.
        motion_lc : `~lightkurve.lightcurve.LightCurve`
            Light curve object with the motion model removed by the corrector.
        gp_lc : `~lightkurve.lightcurve.LightCurve`
            Light curve object containing GP model of the stellar signal.

        Raises
        ------
        RuntimeError : if no `solution_or_trace` has been passed and the object's
            `optimize()` or `sample()` methods have not yet been called.
        """
        if solution_or_trace is None:
            if self.most_recent_solution is None and self.most_recent_trace is None:
                raise RuntimeError("You need to call the `optimize()` or "
                                   "`sample()` methods first.")
            elif self.most_recent_trace is None:
                solution_or_trace = self.most_recent_solution
            else:
                solution_or_trace = self.most_recent_trace

        # Use the most recent trace if available to create corrected and motion
        # model light curves, otherwise use the most recent solution
        corrected_lc, motion_lc = [self._lightcurve_from_solution(solution_or_trace, variable=variable)
                                   for variable in ['corrected_flux', 'motion_model']]
        # Always use the most recent solution for the GP light curve because it isn't sampled
        gp_lc = self._gp_from_solution(self.most_recent_solution)

        return corrected_lc, motion_lc, gp_lc

    def plot_distributions(self):
        varnames = ['logrho', 'logsigma', 'mean']
        model = self.most_recent_model
        map_soln = self.most_recent_solution

        with plt.style.context(MPLSTYLE):
            fig, ax = plt.subplots(1, len(varnames), figsize=(len(varnames)*5, 5), sharey=True)
            for idx, var in enumerate(varnames):

                # Normal Distributions
                if isinstance(model[var].distribution, pm.distributions.continuous.Normal):
                    m, s, tv = model[var].distribution.mu.eval(), model[var].distribution.sd.eval(), model[var].distribution.testval
                    if tv is None:
                        tv = m
                    x = np.linspace(m-s*5, m+s*5, 100)
                    ax[idx].plot(x, Gaussian1D(1, m, s)(x), c='k')

                # Uniform Distributions
                elif isinstance(model[var].distribution, pm.distributions.continuous.Uniform):
                    l, u, tv = model[var].distribution.lower.eval(), model[var].distribution.upper.eval(), model[var].distribution.testval
                    if tv is None:
                        tv = np.mean([u, l])
                    w = (u - l)/2
                    x = np.linspace(l - w, u + w, 100)
                    ax[idx].plot(x, Box1D(1, np.mean([u, l]), w*2)(x), c='k')

                else:
                    raise ValueError("I don't understand this distribution: {}, {}".format(var, model[var].distribution.__class__))

                ax[idx].set_title(var, fontsize=12)
                ax[idx].axvline(tv, ls='--', c='r', label='Init: {0:4.4}'.format(tv), lw=2)
                ax[idx].axvline(map_soln[var], ls='--', c='g', label='MAP: {0:4.4}'.format(map_soln[var]), lw=2)
                ax[idx].fill_between([tv, map_soln[var]], 0, 1, color='b', alpha=0.2, label='diff: {0:4.4}'.format(tv - map_soln[var]))
                ax[idx].legend()
                ax[idx].set_yticks([]);

        return fig



    def plot_diagnostics(self, solution_or_trace=None):
        """Plots a series of useful figures to help understand the noise removal
        process.

        Parameters
        ----------
        solution_or_trace : dict or `pymc3.backends.base.MultiTrace`
            The output returned by this object's `optimize()` or `sample()`
            methods.  If `None`, then the solution most recently computed
            by those methods will be used.

        Returns
        -------
        ax : matplotlib.axes._subplots.AxesSubplot
            The matplotlib axes object.
        """
        # Generate diagnostic light curves
        corrected_lc, motion_lc, gp_lc = self.get_diagnostic_lightcurves(solution_or_trace)

        fig, ax = plt.subplots(3, sharex=True, figsize=(8.485, 10))
        # Plot the corrected light curve over the raw flux
        self.raw_lc.scatter(c='r', alpha=0.3, ax=ax[0], label='Raw Flux', normalize=False)
        corrected_lc.scatter(c='k', ax=ax[0], label='Corrected Flux', normalize=False)

        # Plot the following diagnostics on separate axes from the raw flux
        ax1 = ax[1].twinx()
        y_range = 5 * np.std(self.raw_lc.flux)
        ax[1].set_ylim([np.mean(self.raw_lc.flux) - y_range, np.mean(self.raw_lc.flux) + y_range])
        ax1.set_ylim([np.mean(gp_lc.flux) - y_range, np.mean(gp_lc.flux) + y_range])

        # Plot the stellar model over the raw flux, indicating masked cadences
        self.raw_lc.scatter(c='r', alpha=0.3, ax=ax[1], label='Raw Flux', ylabel='Raw Flux', normalize=False)
        gp_lc.plot(c='k', ax=ax1, label='GP Model', ylabel='GP Model Flux', normalize=False)
        if len(gp_lc[~self.most_recent_model.cadence_mask].flux) > 0:
            gp_lc[~self.most_recent_model.cadence_mask].scatter(ax=ax[1], label='Masked Cadences',
                                                                marker='d', normalize=False)

        # Plot the motion model over the raw light curve
        ax2 = ax[2].twinx()
        ax[2].set_ylim([np.mean(self.raw_lc.flux) - y_range, np.mean(self.raw_lc.flux) + y_range])
        ax2.set_ylim([np.mean(motion_lc.flux) - y_range, np.mean(motion_lc.flux) + y_range])

        self.raw_lc.scatter(c='r', alpha=0.3, ax=ax[2], label='Raw Flux', ylabel='Raw Flux', normalize=False)
        # Add the mean of the raw flux to plot them at the same y-value
        motion_lc.scatter(c='k', ax=ax2, label='Noise Model', ylabel='Motion Model Flux', normalize=False)

        return ax

    def plot_design_matrix(self, design_matrix=None, **kwargs):
        """Plots the design matrix.

        Parameters
        ----------
        design_matrix : np.ndarray
            Matrix of shape (n_cadences, n_regressors) used to create the
            motion model.  If `None`, then the output of this object's
            `~lightkurve.correctors.PLDCorrector.create_design_matrix` method
            will be used.
        **kwargs : dict
            Dictionary of arguments to be passed to
            `~lightkurve.correctors.PLDCorrector.create_design_matrix`.

        Returns
        -------
        ax : matplotlib.axes._subplots.AxesSubplot
            The matplotlib axes object.
        """
        if design_matrix is None:
            design_matrix = self.create_design_matrix(**kwargs)
        with plt.style.context(MPLSTYLE):
            fig, ax = plt.subplots(1, figsize=(8.485, 8.485))
            ax.imshow(design_matrix, aspect='auto')
            ax.set_ylabel('Cadence Number')
            ax.set_xlabel('Regressors')
        return ax

    def plot_weights(self, solution_or_trace=None):
        """Plot the weights on the design matrix.

        Parameters
        ----------
        solution_or_trace : dict or `pymc3.backends.base.MultiTrace`
            The output returned by this object's `optimize()` or `sample()`
            methods.  If `None`, then the solution most recently computed
            by those methods will be used.

        Returns
        -------
        ax : matplotlib.axes._subplots.AxesSubplot
            The matplotlib axes object.
        """
        if solution_or_trace is None:
            if self.most_recent_solution is None and self.most_recent_trace is None:
                raise RuntimeError("You need to call the `optimize()` or "
                                   "`sample()` methods first.")
            elif self.most_recent_trace is None:
                solution_or_trace = self.most_recent_solution
            else:
                solution_or_trace = self.most_recent_trace

        if isinstance(solution_or_trace, pm.backends.base.MultiTrace):
            weights = np.nanmean(solution_or_trace['weights'], axis=0)
        else:
            weights = solution_or_trace['weights']

        with plt.style.context(MPLSTYLE):
            fig, ax = plt.subplots(1, figsize=(8.485, 4.242))
            ax.plot(weights)
            ax.set_ylabel('Weight')
            ax.set_xlabel('Basis Vector')
        return ax

    def plot_weights_and_matrix(self, solution_or_trace=None, design_matrix=None, **kwargs):
        """Visualize both the design matrix elements and their respective weights.
        """
        if design_matrix is None:
            design_matrix = self.create_design_matrix(**kwargs)

        if solution_or_trace is None:
            if self.most_recent_solution is None and self.most_recent_trace is None:
                raise RuntimeError("You need to call the `optimize()` or "
                                   "`sample()` methods first.")
            elif self.most_recent_trace is None:
                solution_or_trace = self.most_recent_solution
            else:
                solution_or_trace = self.most_recent_trace

        if isinstance(solution_or_trace, pm.backends.base.MultiTrace):
            weights = np.nanmean(solution_or_trace['weights'], axis=0)
        else:
            weights = solution_or_trace['weights']

        with plt.style.context(MPLSTYLE):
            fig, ax = plt.subplots(2, figsize=(8.485, 8.485), sharex=True, gridspec_kw={'height_ratios': [1, 2]})
            ax[0].plot(weights)
            ax[0].set_ylabel('Weight')
            ax[1].imshow(np.log(design_matrix), aspect='auto')
            ax[1].set_ylabel('Cadence Number')
            ax[1].set_xlabel('Regressors (log intensity)')
            fig.subplots_adjust(wspace=0)
            fig.tight_layout()
        return ax
