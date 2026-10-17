"""MonKap: a dataset workbench (frame a part of a picture, save it as a new training picture, caption it)
for Fizgig's web UI. The routes are in fizgig/web/monkap_api.py and the page in web/static/monkap*.

This package is the part that touches files, with no web server or GPU in it:

  naming    - the output-name template and the next free number
  ledger    - every crop name ever issued, and where each crop came from (`<dataset>/.monkap/`)
  crops     - a pixel-exact crop of the ORIGINAL file, written atomically and verified
  captions  - reading and saving caption files, the clear-on-purpose rule, Kapture caption modes
  jobs      - Kapture and permanent delete as plain functions
  cache     - the trainer's latent cache, as far as a delete touches it
  settings  - the user's choices
"""
