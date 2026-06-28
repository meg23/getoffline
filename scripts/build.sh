pex --sources-directory=$PWD/src -r $PWD/src/requirements.txt \
    -o ./bin/getthem \
    -m workers.main \
    --venv append
