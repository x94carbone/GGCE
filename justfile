print-version:
    @echo "Current version is:" `uvx --with hatch hatch version`
    

[confirm]
apply-version *VERSION: print-version
    uvx --with hatch hatch version {{ VERSION }}
    sed -n "s/__version__ = '\(.*\)'/\\1/p" ggce/_version.py > .version.tmp
    git add ggce/_version.py
    uv lock --upgrade-package ggce
    git add uv.lock
    git commit -m "Bump version to $(cat .version.tmp)"
    if [ {{ VERSION }} != "dev" ]; then git tag -a "v$(cat .version.tmp)" -m "Bump version to $(cat .version.tmp)"; fi
    rm .version.tmp

serve-jupyter:
    uv run --with=ipython,jupyterlab,matplotlib,seaborn,h5netcdf,netcdf4,scikit-learn,scipy,xarray jupyter lab --notebook-dir="~"

run-ipython:
    uv run ipython

