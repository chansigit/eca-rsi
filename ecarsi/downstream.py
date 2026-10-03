"""Read an h5ad's obs, var and raw counts without loading the matrix (`_data`)."""
from __future__ import annotations


class _Matrix:
    """Read metadata and slice counts directly; AnnData backed mode loads layers."""
    def __init__(self, path, min_vars=2):
        import h5py
        from anndata.io import read_elem, sparse_dataset
        self.file = h5py.File(path, 'r')
        try:
            self.obs = read_elem(self.file['obs'])
            self.obs_names = self.obs.index
            self.var_names = read_elem(self.file['var']).index
            self.n_obs, self.n_vars = len(self.obs_names), len(self.var_names)
            self.shape = (self.n_obs, self.n_vars)
            if 'counts' not in self.file.get('layers', {}):
                raise ValueError('required raw counts layer is missing; X is not a counts fallback')
            node = self.file['layers']['counts']
            self.counts = sparse_dataset(node) if isinstance(node, h5py.Group) else node
            self.layers = {'counts': self.counts}
            if not self.obs_names.is_unique or not self.var_names.is_unique or self.n_obs == 0 or self.n_vars < min_vars:
                raise ValueError(f'invalid dimensions or IDs: {path}')
            if self.counts.shape != (self.n_obs, self.n_vars):
                raise ValueError('counts shape does not match metadata')
        except BaseException:
            self.file.close()
            raise

    def rows(self, indices):
        import numpy as np
        indices = np.asarray(indices)
        order = np.argsort(indices)
        block = self.counts[indices[order], :]
        return block[np.argsort(order), :]


def _data(path, *, min_vars=2):
    return _Matrix(path, min_vars=min_vars)
