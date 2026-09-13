"""Opt-in Slurm compute pool; importing ecarsi does not require Dask."""


def main(argv=None):
    from .__main__ import main as run
    return run(argv)
