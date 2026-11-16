# LDAP

django-auth-ldap needs the python-ldap package.

The cp37 wheels that used to live in this directory are not an install path.
requirements-ldap-win.txt installs current python-ldap from PyPI. Building it
on Windows needs OpenLDAP headers (or a wheel built for that CPython).
https://www.python-ldap.org/en/python-ldap-3.4.4/installing.html
