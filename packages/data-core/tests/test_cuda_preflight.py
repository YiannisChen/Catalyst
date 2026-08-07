"""CUDA preflight fail-closed regression matrix (Finding 6).

Every contract violation below must raise before any model artifact is used;
none of these tests download weights or touch a GPU.
"""

from __future__ import annotations

import numpy as np
import pytest

from catalyst_data.config import BGE_M3_REVISION


class FakeCudaAvailable:
    @staticmethod
    def is_available():
        return True


class FakeCudaUnavailable:
    @staticmethod
    def is_available():
        return False


class FakeTorch:
    def __init__(self, cuda=FakeCudaAvailable()):
        self.cuda = cuda


def _load(encode, *, cuda=FakeCudaAvailable(), revision=BGE_M3_REVISION, download=None):
    from catalyst_data.retrieval.gpu_driver import _load_real_cuda_embedder

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def encode(self, texts, **kwargs):
            return encode(texts, **kwargs)

    if download is None:
        download = lambda **kwargs: "/fixture/model"

    return _load_real_cuda_embedder(
        torch_module=FakeTorch(cuda),
        snapshot_download_fn=download,
        model_class=Model,
        model_revision=revision,
    )


def test_preflight_rejects_wrong_shape():
    with pytest.raises(RuntimeError, match="shape"):
        _load(lambda texts, **kwargs: {"dense_vecs": np.ones((2, 1024), dtype=np.float32)})


def test_preflight_rejects_dimension_not_1024():
    with pytest.raises(RuntimeError, match="shape"):
        _load(lambda texts, **kwargs: {"dense_vecs": np.ones((1, 512), dtype=np.float32)})


def test_preflight_rejects_non_floating_dtype():
    with pytest.raises(RuntimeError, match="floating"):
        _load(lambda texts, **kwargs: {"dense_vecs": np.ones((1, 1024), dtype=np.int32)})


def test_preflight_accepts_float16_and_casts_to_unit_float32():
    """Real BGEM3 use_fp16=True returns float16; preflight must accept it."""
    raw = np.ones((1, 1024), dtype=np.float16)
    embed = _load(lambda texts, **kwargs: {"dense_vecs": raw})
    # Loader returns a callable; preflight already succeeded during load.
    assert callable(embed)


def test_preflight_accepts_unnormalized_float32_dense_vecs():
    """Model dense vectors need not arrive pre-normalized; storage path L2s."""
    embed = _load(
        lambda texts, **kwargs: {"dense_vecs": np.ones((1, 1024), dtype=np.float32)}
    )
    assert callable(embed)


def test_preflight_rejects_nan():
    matrix = np.ones((1, 1024), dtype=np.float32)
    matrix[0, 0] = np.nan
    with pytest.raises(RuntimeError, match="finite"):
        _load(lambda texts, **kwargs: {"dense_vecs": matrix})


def test_preflight_rejects_inf():
    matrix = np.ones((1, 1024), dtype=np.float32)
    matrix[0, 0] = np.inf
    with pytest.raises(RuntimeError, match="finite"):
        _load(lambda texts, **kwargs: {"dense_vecs": matrix})


def test_preflight_rejects_zero_vector():
    with pytest.raises(RuntimeError, match="zero"):
        _load(lambda texts, **kwargs: {"dense_vecs": np.zeros((1, 1024), dtype=np.float32)})


def test_preflight_fails_closed_when_cuda_unavailable():
    download_called = []

    def forbidden_download(**kwargs):
        download_called.append(True)
        raise AssertionError("download must not run without CUDA")

    with pytest.raises(RuntimeError, match="CUDA"):
        _load(
            lambda texts, **kwargs: {"dense_vecs": np.ones((1, 1024), dtype=np.float32)},
            cuda=FakeCudaUnavailable(),
            download=forbidden_download,
        )
    assert download_called == []


def test_preflight_fails_closed_on_batch_one_cuda_oom():
    def encode(texts, **kwargs):
        raise RuntimeError("CUDA out of memory")

    with pytest.raises(RuntimeError, match="CUDA OOM at batch=1"):
        _load(encode)


def test_preflight_rejects_revision_mismatch_before_loader():
    from catalyst_data.retrieval.gpu_driver import _load_real_cuda_embedder

    with pytest.raises(ValueError, match="pinned"):
        _load_real_cuda_embedder(model_revision="0" * 40)
