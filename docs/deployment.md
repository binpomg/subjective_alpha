# Deployment

The repository is a framework. Choose a private checkout directory and keep
all A-share files, Polymarket history, generated outputs and credentials
outside version control. The commands below use a relative project root and do
not assume a particular host or username.

```bash
cd <PROJECT_ROOT>
.venv/bin/python -m poly_ashare.cli validate-config --config config/experiment_config.remote.json
.venv/bin/python -m unittest discover -s tests -v
```

The model remains disabled in the example configuration. Historical data must
be cached privately before snapshots are built; snapshot commands should read
that cache rather than re-requesting an API during a replay. Set the private
data locations through a local, ignored configuration or environment variables.

Do not start a service or change `model.execution` until the model adapter,
prompt version, budget, isolation policy and output review have been frozen.
