import re

import satudatascape


def test_package_imports_and_has_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", satudatascape.__version__)
