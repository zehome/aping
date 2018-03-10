# -*- coding: utf-8 -*-

from setuptools import setup
setup(
    name="aping",
    entry_points={
        'console_scripts': [
            'aping = aping.main:main',
            'aping-smokepingimport = aping.smokeping:main'
        ]
    }
)
