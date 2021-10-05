import copy
import warnings
import numpy as np
from collections import namedtuple
from .core import CallbackBase, CollectThenCompute


class LiveFit(CallbackBase):
    """
    Fit a model to data using nonlinear least-squares minimization.

    Parameters
    ----------
    model : lmfit.Model
    y : string
        name of the field in the Event document that is the dependent variable
    independent_vars : dict
        map the independent variable name(s) in the model to the field(s)
        in the Event document; e.g., ``{'x': 'motor'}``
    init_guess : dict, optional
        initial guesses for other values, if expected by model;
        e.g., ``{'sigma': 1}``
    update_every : int or None, optional
        How often to recompute the fit. If `None`, do not compute until the
        end. Default is 1 (recompute after each new point).

    Attributes
    ----------
    result : lmfit.ModelResult
    """
    def __init__(self, model, y, independent_vars, init_guess=None, *,
                 update_every=1):
        self.ydata = []
        self.independent_vars_data = {}
        self.__stale = False
        self.result = None
        self._model = model
        self.y = y
        self.independent_vars = independent_vars
        if init_guess is None:
            init_guess = {}
        self.init_guess = init_guess
        self.update_every = update_every

    @property
    def model(self):
        # Make this a property so it can't be updated.
        return self._model

    @property
    def independent_vars(self):
        return self._independent_vars

    @independent_vars.setter
    def independent_vars(self, val):
        if set(val) != set(self.model.independent_vars):
            raise ValueError("keys {} must match the independent variables in "
                             "the model "
                             "{}".format(set(val),
                                         set(self.model.independent_vars)))
        self._independent_vars = val
        self.independent_vars_data.clear()
        self.independent_vars_data.update({k: [] for k in val})
        self._reset()

    def _reset(self):
        self.result = None
        self.__stale = False
        self.ydata.clear()
        for v in self.independent_vars_data.values():
            v.clear()

    def start(self, doc):
        self._reset()
        super().start(doc)

    def event(self, doc):
        if self.y not in doc['data']:
            return
        y = doc['data'][self.y]
        idv = {k: doc['data'][v] for k, v in self.independent_vars.items()}

        # Always stash the data for the next time the fit is updated.
        self.update_caches(y, idv)
        self.__stale = True

        # Maybe update the fit or maybe wait.
        if self.update_every is not None:
            i = len(self.ydata)
            N = len(self.model.param_names)
            if i < N:
                # not enough points to fit yet
                pass
            elif (i == N) or ((i - 1) % self.update_every == 0):
                self.update_fit()
        super().event(doc)

    def stop(self, doc):
        # Update the fit if it was not updated by the last event.
        if self.__stale:
            self.update_fit()
        super().stop(doc)

    def update_caches(self, y, independent_vars):
        self.ydata.append(y)
        for k, v in self.independent_vars_data.items():
            v.append(independent_vars[k])

    def update_fit(self):
        N = len(self.model.param_names)
        if len(self.ydata) < N:
            warnings.warn("LiveFitPlot cannot update fit until there are at least {} "
                          "data points".format(N))
        else:
            kwargs = {}
            kwargs.update(self.independent_vars_data)
            kwargs.update(self.init_guess)
            self.result = self.model.fit(self.ydata, **kwargs)
            self.__stale = False


# This function is vendored from scipy v0.16.1 to avoid adding a scipy
# dependency just for one Python function


def center_of_mass(input, labels=None, index=None):
    """
    Calculate the center of mass of the values of an array at labels.
    Parameters
    ----------
    input : ndarray
        Data from which to calculate center-of-mass.
    labels : ndarray, optional
        Labels for objects in `input`, as generated by `ndimage.label`.
        Only used with `index`.  Dimensions must be the same as `input`.
    index : int or sequence of ints, optional
        Labels for which to calculate centers-of-mass. If not specified,
        all labels greater than zero are used.  Only used with `labels`.
    Returns
    -------
    center_of_mass : tuple, or list of tuples
        Coordinates of centers-of-mass.
    Examples
    --------
    >>> a = np.array(([0,0,0,0],
                      [0,1,1,0],
                      [0,1,1,0],
                      [0,1,1,0]))
    >>> from scipy import ndimage
    >>> ndimage.measurements.center_of_mass(a)
    (2.0, 1.5)
    Calculation of multiple objects in an image
    >>> b = np.array(([0,1,1,0],
                      [0,1,0,0],
                      [0,0,0,0],
                      [0,0,1,1],
                      [0,0,1,1]))
    >>> lbl = ndimage.label(b)[0]
    >>> ndimage.measurements.center_of_mass(b, lbl, [1,2])
    [(0.33333333333333331, 1.3333333333333333), (3.5, 2.5)]
    """
    normalizer = np.sum(input, labels, index)
    grids = np.ogrid[[slice(0, i) for i in input.shape]]

    results = [
        np.sum(input * grids[dir].astype(float), labels, index) / normalizer
        for dir in range(input.ndim)]

    if np.isscalar(results[0]):
        return tuple(results)

    return [tuple(v) for v in np.array(results).T]


class PeakStats(CollectThenCompute):
    """
    Compute peak statsitics after a run finishes.

    Results are stored in the attributes.

    Parameters
    ----------
    x : string
        field name for the x variable (e.g., a motor)
    y : string
        field name for the y variable (e.g., a detector)
    compute_derivative : bool, optional
        compute stats for derivative of the readings. False by default.
    edge_count : int or None, optional
        If not None, number of points at beginning and end to use
        for quick and dirty background subtraction.

    Notes
    -----
    It is assumed that the two fields, x and y, are recorded in the same
    Event stream.

    Attributes
    ----------
    com : center of mass
    cen : mid-point between half-max points on each side of the peak
    max : x location of y maximum
    min : x location of y minimum
    crossings : crosses between y and middle line, which is
          ((np.max(y) + np.min(y)) / 2). Users can estimate FWHM based
          on those info.
    fwhm : the computed full width half maximum (fwhm) of a peak.
           The distance between the first and last crossing is taken to
           be the fwhm.
    """

    def __init__(self, x, y, edge_count=None, compute_derivative=False):
        self.x = x
        self.y = y
        self._edge_count = edge_count
        self.compute_derivative = compute_derivative

        self._stats_fields = {
            "min": None,
            "max": None,
            "com": None,
            "cen": None,
            "crossings": None,
            "fwhm": None,
            "lin_bnk": None,
        }
        for field, value in self._stats_fields.items():
            setattr(self, field, value)

        if self.compute_derivative:
            self.der = None

        super().__init__()

    def __getitem__(self, key):
        if key in self._stats_fields.keys():
            return getattr(self, key)
        else:
            raise KeyError

    @staticmethod
    def _calc_stats(x, y, fields, edge_count=None):
        if edge_count is not None:
            left_x = np.mean(x[:edge_count])
            left_y = np.mean(y[:edge_count])

            right_x = np.mean(x[-edge_count:])
            right_y = np.mean(y[-edge_count:])

            m = (right_y - left_y) / (right_x - left_x)
            b = left_y - m * left_x
            y = y - (m * x + b)
            fields["lin_bkg"] = {"m": m, "b": b}

        fields["min"] = (x[np.argmin(y)], y[np.argmin(y)])
        fields["max"] = (x[np.argmax(y)], y[np.argmax(y)])
        fields["com"] = (np.interp(center_of_mass(y), np.arange(len(x)), x))
        mid = (np.max(y) + np.min(y)) / 2
        crossings = np.where(np.diff((y > mid).astype(np.int)))[0]
        _cen_list = []
        for cr in crossings.ravel():
            _x = x[cr:cr + 2]
            _y = y[cr:cr + 2] - mid

            dx = np.diff(_x)[0]
            dy = np.diff(_y)[0]
            m = dy / dx
            _cen_list.append((-_y[0] / m) + _x[0])

        if _cen_list:
            fields["cen"] = np.mean(_cen_list)
            fields["crossings"] = np.array(_cen_list)
            if len(_cen_list) >= 2:
                fields["fwhm"] = np.abs(fields["crossings"][-1] - fields["crossings"][0],
                                        dtype=float)

        Stats = namedtuple("Stats", field_names=fields.keys(),
                           defaults=fields.values())
        stats = Stats()
        return stats

    def compute(self):
        "This method is called at run-stop time by the superclass."
        # clear all results
        for field, value in self._stats_fields.items():
            setattr(self, field, value)

        x = []
        y = []
        for event in self._events:
            try:
                _x = event['data'][self.x]
                _y = event['data'][self.y]
            except KeyError:
                pass
            else:
                x.append(_x)
                y.append(_y)
        x = np.array(x)
        y = np.array(y)

        if not len(x):
            # nothing to do
            return
        self.x_data = x
        self.y_data = y

        stats_fields = copy.deepcopy(self._stats_fields)
        stats = self._calc_stats(x, y, stats_fields,
                                      edge_count=self._edge_count)

        for field in self._stats_fields:
            setattr(self, field, getattr(stats, field))

        if self.compute_derivative:
            # Calculate the derivative stats of the data
            x_der = x[1:]
            y_der = np.diff(y)

            stats_fields = copy.deepcopy(self._stats_fields)
            stats_fields.update({"x": x_der, "y": y_der})
            self.der = self._calc_stats(
                x_der, y_der,
                stats_fields,
                edge_count=self._edge_count
            )

        # reset y data
        y = self.y_data
