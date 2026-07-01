from setuptools import setup
from Cython.Build import cythonize

setup(
    ext_modules = cythonize("Avbuds_AI_translate.py", compiler_directives={'language_level': "3"})
)