import shutil

from crosscheck import JAR


def pytest_report_header(config):
    if JAR.exists() and shutil.which('java'):
        return f'FitCSVTool validation: enabled ({JAR})'
    return 'FitCSVTool validation: disabled (run tools/fetch_fitcsvtool.py; needs Java)'
