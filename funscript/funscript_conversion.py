import math

import numpy as np
from funscript.funscript import Funscript


_MAX_SEGMENT_POINTS = 6
_SEGMENT_PENALTY = 1e-4
_POSITION_TOLERANCE = 0.011
_TIME_TOLERANCE_MS = 1
_DURATION_BOUNDS_MS = {
    1: (0, 0),
    2: (1, 100),
    3: (101, 200),
    4: (201, 300),
    5: (301, 400),
    6: (401, None),
}


def convert_1d_to_2d(funscript: Funscript, random_direction_change_probability=0.1):
    at, pos = funscript.x, funscript.y

    dir = 1

    t_out = []
    x_out = []
    y_out = []

    for i in range(len(pos) - 1):
        start_t, end_t = at[i:i + 2]
        start_p, end_p = pos[i:i + 2]

        duration = end_t - start_t
        if start_p == end_p:
            n = 1
        else:
            if duration <= .100:
                n = 2
            elif duration <= .200:
                n = 3
            elif duration <= .300:
                n = 4
            elif duration <= .400:
                n = 5
            else:
                n = 6

        t = np.linspace(0.0, duration, n, endpoint=False)
        theta = np.linspace(0, np.pi, n, endpoint=False)
        center = (end_p + start_p) / 2
        r = (start_p - end_p) / 2

        if np.random.random() < random_direction_change_probability:
            dir = dir * -1

        x = center + r * np.cos(theta)
        y = r * dir * np.sin(theta) + 0.5
        t_out += list(t + start_t)
        x_out += list(x)
        y_out += list(y)

    return t_out, x_out, y_out


def convert_2d_to_1d(alpha_funscript: Funscript, beta_funscript: Funscript):
    """Best-effort inverse of convert_1d_to_2d for alpha/beta pairs it produced.

    Returns a tuple of (funscript, warnings).

    Limitation: if the original script ended with a flat segment, the forward
    converter collapsed that final duration into a single sample. In that case
    the recovered script cannot know the original final timestamp and emits a
    warning.
    """

    _validate_2d_pair(alpha_funscript, beta_funscript)

    times_ms = [int(round(float(value) * 1000.0)) for value in alpha_funscript.x]
    alpha_values = [float(value) for value in alpha_funscript.y]
    beta_values = [float(value) for value in beta_funscript.y]

    if not times_ms:
        return Funscript([], []), []

    best_cost = [math.inf] * (len(times_ms) + 1)
    best_cost[0] = 0.0
    previous_segment = [None] * (len(times_ms) + 1)

    for start_index in range(len(times_ms)):
        if math.isinf(best_cost[start_index]):
            continue

        max_segment_points = min(_MAX_SEGMENT_POINTS, len(times_ms) - start_index)
        for segment_points in range(1, max_segment_points + 1):
            segment_fit = _fit_segment(times_ms, alpha_values, beta_values, start_index, segment_points)
            if segment_fit is None:
                continue

            next_index = start_index + segment_points
            candidate_cost = best_cost[start_index] + segment_fit['cost'] + _SEGMENT_PENALTY
            if candidate_cost < best_cost[next_index]:
                best_cost[next_index] = candidate_cost
                previous_segment[next_index] = (start_index, segment_points, segment_fit)

    if previous_segment[-1] is None:
        raise ValueError('Unable to reconstruct a 1d funscript from the provided alpha/beta pair.')

    segments = []
    cursor = len(times_ms)
    while cursor > 0:
        segment = previous_segment[cursor]
        segments.append(segment)
        cursor = segment[0]
    segments.reverse()

    recovered_times_ms = []
    recovered_positions = []
    warnings = []

    for start_index, _, segment_fit in segments:
        recovered_times_ms.append(times_ms[start_index])
        recovered_positions.append(alpha_values[start_index])
        warnings.extend(segment_fit['warnings'])

    last_segment = segments[-1][2]
    if last_segment['end_time_ms'] is not None and last_segment['end_position'] is not None:
        recovered_times_ms.append(last_segment['end_time_ms'])
        recovered_positions.append(last_segment['end_position'])

    recovered_times = [time_ms / 1000.0 for time_ms in recovered_times_ms]
    return Funscript(recovered_times, recovered_positions), warnings


def _validate_2d_pair(alpha_funscript: Funscript, beta_funscript: Funscript):
    if len(alpha_funscript.x) != len(beta_funscript.x):
        raise ValueError('Alpha and beta funscripts must have the same number of actions.')

    for alpha_time, beta_time in zip(alpha_funscript.x, beta_funscript.x):
        if abs(float(alpha_time) - float(beta_time)) > (_TIME_TOLERANCE_MS / 1000.0):
            raise ValueError('Alpha and beta funscripts must have matching timestamps.')


def _fit_segment(times_ms, alpha_values, beta_values, start_index, segment_points):
    is_last_segment = (start_index + segment_points) == len(times_ms)
    start_time_ms = times_ms[start_index]
    start_position = alpha_values[start_index]
    actual_times = times_ms[start_index:start_index + segment_points]
    actual_alpha = alpha_values[start_index:start_index + segment_points]
    actual_beta = beta_values[start_index:start_index + segment_points]

    warnings = []

    if segment_points == 1:
        if abs(actual_beta[0] - 0.5) > _POSITION_TOLERANCE:
            return None
        if is_last_segment:
            warnings.append(
                'Trailing flat segment duration could not be recovered exactly; the output omits the lost final hold time.'
            )
            return {
                'cost': abs(actual_beta[0] - 0.5),
                'end_time_ms': None,
                'end_position': None,
                'warnings': warnings,
            }

        end_position = alpha_values[start_index + 1]
        if abs(start_position - end_position) > _POSITION_TOLERANCE:
            return None
        return {
            'cost': abs(actual_beta[0] - 0.5) + abs(start_position - end_position),
            'end_time_ms': None,
            'end_position': None,
            'warnings': warnings,
        }

    if is_last_segment:
        end_time_ms, duration_warnings = _infer_last_segment_end_time_ms(actual_times, segment_points)
        if end_time_ms is None:
            return None
        candidate_end_positions = _candidate_last_segment_end_positions(actual_alpha)
        warnings.extend(duration_warnings)
    else:
        end_time_ms = times_ms[start_index + segment_points]
        end_position = alpha_values[start_index + segment_points]
        candidate_end_positions = [end_position]

    duration_ms = end_time_ms - start_time_ms
    if duration_ms <= 0:
        return None

    expected_times = [
        _quantize_time_ms(start_time_ms, duration_ms, segment_points, sample_index)
        for sample_index in range(segment_points)
    ]
    if any(abs(expected - actual) > _TIME_TOLERANCE_MS for expected, actual in zip(expected_times, actual_times)):
        return None

    best_score = math.inf
    best_end_position = None
    for end_position in candidate_end_positions:
        center = (start_position + end_position) / 2.0
        radius = (start_position - end_position) / 2.0

        for direction in (-1.0, 1.0):
            score = 0.0
            for sample_index in range(segment_points):
                theta = sample_index * math.pi / segment_points
                predicted_alpha = _quantize_position(center + radius * math.cos(theta))
                predicted_beta = _quantize_position(radius * direction * math.sin(theta) + 0.5)

                alpha_error = abs(predicted_alpha - actual_alpha[sample_index])
                beta_error = abs(predicted_beta - actual_beta[sample_index])
                if alpha_error > _POSITION_TOLERANCE or beta_error > _POSITION_TOLERANCE:
                    score = math.inf
                    break
                score += alpha_error + beta_error

            if score < best_score:
                best_score = score
                best_end_position = end_position

    if math.isinf(best_score):
        return None

    return {
        'cost': best_score,
        'end_time_ms': end_time_ms,
        'end_position': best_end_position,
        'warnings': warnings,
    }


def _infer_last_segment_end_time_ms(segment_times_ms, segment_points):
    duration_min_ms, duration_max_ms = _DURATION_BOUNDS_MS[segment_points]
    start_time_ms = segment_times_ms[0]
    if duration_max_ms is None:
        approx_duration_ms = round((segment_times_ms[-1] - start_time_ms) * segment_points / (segment_points - 1))
        search_start = max(duration_min_ms, approx_duration_ms - segment_points * 6)
        search_end = max(search_start, approx_duration_ms + segment_points * 6)
    else:
        search_start = duration_min_ms
        search_end = duration_max_ms

    best_duration_ms = None
    best_score = None
    tied_durations = []

    for duration_ms in range(search_start, search_end + 1):
        predicted_times = [
            _quantize_time_ms(start_time_ms, duration_ms, segment_points, sample_index)
            for sample_index in range(segment_points)
        ]
        errors = [abs(predicted - observed) for predicted, observed in zip(predicted_times, segment_times_ms)]
        score = (max(errors), sum(errors), duration_ms)

        if best_score is None or score < best_score:
            best_score = score
            best_duration_ms = duration_ms
            tied_durations = [duration_ms]
        elif score[:2] == best_score[:2]:
            tied_durations.append(duration_ms)

    if best_score is None or best_score[0] > _TIME_TOLERANCE_MS:
        return None, []

    warnings = []
    if len(tied_durations) > 1:
        warnings.append(
            f'Final segment duration was ambiguous after quantization; using the shortest consistent duration of {best_duration_ms} ms.'
        )

    return start_time_ms + best_duration_ms, warnings


def _candidate_last_segment_end_positions(alpha_segment):
    segment_points = len(alpha_segment)
    cos_values = np.cos(np.arange(segment_points) * math.pi / segment_points)
    fit_matrix = np.column_stack([np.ones(segment_points), cos_values])
    center, radius = np.linalg.lstsq(fit_matrix, np.asarray(alpha_segment, dtype=float), rcond=None)[0]
    estimate = _quantize_position(center - radius)

    candidate_end_positions = []
    lower_bound = max(0.0, estimate - 0.1)
    upper_bound = min(1.0, estimate + 0.1)
    start_index = int(round(lower_bound * 100))
    end_index = int(round(upper_bound * 100))
    for candidate_index in range(start_index, end_index + 1):
        candidate_end_positions.append(candidate_index / 100.0)

    if estimate not in candidate_end_positions:
        candidate_end_positions.append(estimate)

    candidate_end_positions.sort()
    return candidate_end_positions


def _quantize_position(value):
    clamped = min(max(float(value), 0.0), 1.0)
    return math.floor(clamped * 100.0 + 1e-9) / 100.0


def _quantize_time_ms(start_time_ms, duration_ms, segment_points, sample_index):
    return math.floor(start_time_ms + (sample_index * duration_ms / segment_points) + 1e-9)
