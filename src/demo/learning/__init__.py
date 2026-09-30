"""SHADOW-ONLY learning stack (meta-label challengers). Never imported by the trading hot path.

Heavy libraries (scikit-learn, lightgbm, river, mlflow) are imported lazily inside constructors and
live in the optional uv dependency group `learning`.
"""
