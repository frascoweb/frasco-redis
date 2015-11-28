from setuptools import setup, find_packages


setup(
    name='frasco-redis',
    version='0.1.4',
    url='http://github.com/frascoweb/frasco-redis',
    license='MIT',
    author='Maxime Bouroumeau-Fuseau',
    author_email='maxime.bouroumeau@gmail.com',
    description="Redis integration for Frasco",
    packages=find_packages(),
    zip_safe=False,
    platforms='any',
    install_requires=[
        'frasco',
        'redis>=2.10.1'
    ]
)
