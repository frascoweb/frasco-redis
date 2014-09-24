from setuptools import setup


def desc():
    with open("README.md") as f:
        return f.read()

def reqs():
    with open('requirements.txt') as f:
        return f.read().splitlines()

setup(
    name='frasco-redis',
    version='0.1',
    url='http://github.com/frascoweb/frasco-redis',
    license='MIT',
    author='Maxime Bouroumeau-Fuseau',
    author_email='maxime.bouroumeau@gmail.com',
    description="Redis integration for Frasco",
    long_description=desc(),
    py_modules=['frasco_redis'],
    platforms='any',
    install_requires=reqs() + ['frasco']
)