from stim_math.transforms_4 import constrain_4p_amplitudes


def canonicalize_4p_display(a, b, c, d, sensor_widget=None):
    """Return the shared fourphase display state for visualization widgets."""
    if sensor_widget is not None:
        params = {"e1": a, "e2": b, "e3": c, "e4": d}
        sensor_widget.process(params)
        a = params["e1"]
        b = params["e2"]
        c = params["e3"]
        d = params["e4"]

    return tuple(constrain_4p_amplitudes(a, b, c, d))