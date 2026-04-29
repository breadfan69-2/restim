import argparse
from pathlib import Path

from funscript.funscript import Funscript
from funscript.funscript_conversion import convert_2d_to_1d


def _resolve_paths(alpha_path_arg: str, beta_path_arg: str | None, output_path_arg: str | None):
    alpha_path = Path(alpha_path_arg)
    beta_path = Path(beta_path_arg) if beta_path_arg else None

    if beta_path is None:
        if alpha_path.name.endswith('.alpha.funscript'):
            beta_path = alpha_path.with_name(alpha_path.name.replace('.alpha.funscript', '.beta.funscript'))
            output_path = alpha_path.with_name(alpha_path.name.replace('.alpha.funscript', '.funscript'))
        elif alpha_path.name.endswith('.beta.funscript'):
            beta_path = alpha_path
            alpha_path = alpha_path.with_name(alpha_path.name.replace('.beta.funscript', '.alpha.funscript'))
            output_path = beta_path.with_name(beta_path.name.replace('.beta.funscript', '.funscript'))
        else:
            raise ValueError('When beta is omitted, the input file must end with .alpha.funscript or .beta.funscript.')
    else:
        output_path = Path(output_path_arg) if output_path_arg else alpha_path.with_name(alpha_path.name.replace('.alpha.funscript', '.funscript'))

    if output_path_arg:
        output_path = Path(output_path_arg)

    return alpha_path, beta_path, output_path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        prog='funscript 2d to 1d',
        description='Reconstruct a 1d funscript from an alpha/beta pair produced by restim.',
    )
    parser.add_argument('alpha', help='Path to the alpha funscript, or either alpha/beta file if beta is omitted.')
    parser.add_argument('beta', nargs='?', help='Path to the beta funscript.')
    parser.add_argument('-o', '--output', help='Path for the recovered 1d funscript.')

    args = parser.parse_args()

    alpha_path, beta_path, output_path = _resolve_paths(args.alpha, args.beta, args.output)

    print(f'alpha : {alpha_path}')
    print(f'beta  : {beta_path}')
    print(f'output: {output_path}')

    alpha_funscript = Funscript.from_file(alpha_path)
    beta_funscript = Funscript.from_file(beta_path)
    recovered_funscript, warnings = convert_2d_to_1d(alpha_funscript, beta_funscript)
    recovered_funscript.save_to_path(output_path)

    if warnings:
        print('warnings:')
        for warning in warnings:
            print(f'  - {warning}')

    print('done')