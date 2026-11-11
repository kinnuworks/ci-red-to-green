# humanize_mini

The sample project behind `/ci demo`. `humanize_mini/filesize.py` and `tests/test_filesize.py`
are trimmed from [humanize](https://github.com/python-humanize/humanize) (MIT licence, see
`LICENCE`). The source has the real bug that humanize fixed in PR #329: `naturalsize(999999)`
returns `1000.0 kB` instead of `1.0 MB`. The tests are humanize's own.
