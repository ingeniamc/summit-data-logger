#!/usr/bin/env bash
pip install pipenv
python -m pipenv run python -m pip install pip==18.0
python -m pipenv install
mkdir outputs
