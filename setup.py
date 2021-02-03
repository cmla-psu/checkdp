from setuptools import setup, find_packages

# Get the long description from the relevant file
with open('README.md', encoding='utf-8') as f:
    long_description = f.read()

setup(
    name='checkdp',
    version='0.1',
    long_description=long_description,
    license='MIT',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Developers',
        'Topic :: Programming Language :: Differential Privacy',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8'
    ],
    keywords='Programming Language, Differential Privacy',
    packages=find_packages(exclude=['tests']),
    install_requires=['sympy', 'coloredlogs', 'tqdm', 'pycparser', 'pytest'],
    extras_require={
        'test': ['pytest', 'pytest-cov', 'coverage'],
    },
    entry_points={
        'console_scripts': [
            'checkdp=checkdp.__main__:main',
        ],
    },
)
