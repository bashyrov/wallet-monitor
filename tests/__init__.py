"""Marker so `from tests.conftest import _register` resolves to this package
and not to the stray `tests` package that the `binance` dependency ships in
site-packages. Without this, pytest collection explodes with ImportError.
"""
