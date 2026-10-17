"""Tests hash passwords and setup codes with a cheap scrypt cost so the suite
stays fast. The app's own default (auth.SCRYPT) is the strong one; it is kept
here as STRONG_SCRYPT for the test that checks it."""
from app import auth

STRONG_SCRYPT = dict(auth.SCRYPT)
auth.SCRYPT = {"n": 2**10, "r": 8, "p": 1}
