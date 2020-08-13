#!/usr/bin/env python3

import argparse as ap
import json
import logging
import logging.handlers
import socket
import sys

import vistir
from ust_download_cache import USTDownloadCache

import cvescan.constants as const
import cvescan.debug as debug
from cvescan.cvescanner import CVEScanner
from cvescan.errors import ArgumentError, DistribIDError, PkgCountError
from cvescan.local_sysinfo import LocalSysInfo
from cvescan.options import Options
from cvescan.output_formatters import (
    CLIOutputFormatter,
    CSVOutputFormatter,
    CVEOutputFormatter,
    CVEScanResultSorter,
    JSONOutputFormatter,
    NagiosOutputFormatter,
    PackageScanResultSorter,
    SyslogOutputFormatter,
)
from cvescan.target_sysinfo import TargetSysInfo

from .version import get_version


def set_output_verbosity(opt):
    if opt.silent:
        spin.silent = True
        return get_null_logger()

    logger = logging.getLogger(const.STDOUT_LOGGER_NAME)

    if opt.verbose:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    log_formatter = logging.Formatter("%(message)s")
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_formatter)
    logger.addHandler(stream_handler)

    return logger


def get_null_logger():
    logger = logging.getLogger(const.NULL_LOGGER_NAME)
    if not logger.hasHandlers():
        logger.addHandler(logging.NullHandler())

    return logger


def error_exit(msg, code=const.ERROR_RETURN_CODE):
    print("Error: %s" % msg, file=sys.stderr)
    sys.exit(code)


def parse_args():
    cvescan_ap = ap.ArgumentParser(description=const.CVESCAN_DESCRIPTION)
    cvescan_ap.add_argument(
        "--version",
        action="version",
        version="CVEScan, v" + get_version(),
        help=const.VERSION_HELP,
    )
    cvescan_ap.add_argument(
        "-v", "--verbose", action="store_true", default=False, help=const.VERBOSE_HELP
    )
    cvescan_ap.add_argument(
        "-p",
        "--priority",
        help=const.PRIORITY_HELP,
        choices=[const.CRITICAL, const.HIGH, const.MEDIUM, const.ALL],
        default=None,
    )
    cvescan_ap.add_argument("--db", metavar="UBUNTU_DB_FILE", help=const.DB_FILE_HELP)
    cvescan_ap.add_argument(
        "-m", "--manifest", metavar="MANIFEST_FILE", help=const.MANIFEST_HELP
    )
    cvescan_ap.add_argument("--csv", action="store_true", help=const.CSV_HELP)
    cvescan_ap.add_argument("--json", action="store_true", help=const.JSON_HELP)
    cvescan_ap.add_argument("--syslog", metavar="HOST:PORT", help=const.SYSLOG_HELP)
    cvescan_ap.add_argument(
        "--syslog-light", metavar="HOST:PORT", help=const.SYSLOG_LIGHT_HELP
    )
    cvescan_ap.add_argument(
        "--show-links", action="store_true", default=False, help=const.UCT_LINKS_HELP
    )
    cvescan_ap.add_argument(
        "--unresolved", action="store_true", default=False, help=const.UNRESOLVED_HELP
    )
    cvescan_ap.add_argument(
        "-x",
        "--experimental",
        action="store_true",
        default=False,
        help=const.EXPERIMENTAL_HELP,
    )
    cvescan_ap.add_argument(
        "-n", "--nagios", action="store_true", default=False, help=const.NAGIOS_HELP
    )
    cvescan_ap.add_argument(
        "-c", "--cve", metavar="CVE-IDENTIFIER", help=const.CVE_HELP
    )
    cvescan_ap.add_argument(
        "-s", "--silent", action="store_true", default=False, help=const.SILENT_HELP
    )

    return cvescan_ap.parse_args()


def load_output_formatter(opt, logger):
    sorter = load_output_sorter(opt)

    if opt.csv:
        return CSVOutputFormatter(opt, logger, sorter=sorter)

    if opt.cve:
        return CVEOutputFormatter(opt, logger)

    if opt.json:
        return JSONOutputFormatter(opt, logger, sorter=sorter, indent=4)

    if opt.nagios_mode:
        return NagiosOutputFormatter(opt, logger, sorter=sorter)

    if opt.syslog or opt.syslog_light:
        json_output_formatter = JSONOutputFormatter(
            opt, logger, sorter=sorter, indent=None
        )
        return SyslogOutputFormatter(opt, logger, json_output_formatter)

    return CLIOutputFormatter(opt, logger, sorter=sorter)


def load_output_sorter(opt):
    pkg_sorter = PackageScanResultSorter()
    return CVEScanResultSorter(subsorters=[pkg_sorter])


def spin(start_text, ok, fail):
    def spin_decorator(func):
        def wrapper(*args, **kwargs):
            if spin.silent:
                return func(*args, **kwargs)

            with vistir.contextmanagers.spinner(
                start_text=start_text, write_to_stdout=False
            ) as spinner:
                try:
                    return_value = func(*args, **kwargs)
                    spinner.ok(f"✅ {ok}")
                    return return_value
                except Exception as ex:
                    spinner.fail(f"❌ {fail}")
                    raise ex

        return wrapper

    return spin_decorator


spin.silent = False


@spin(
    "Downloading Ubuntu vulnerability database...",
    "Ubuntu vulnerability datbase successfully downloaded!",
    "Download Failed!",
)
def load_uct_data(opt, download_cache, target_sysinfo):
    if opt.download_uct_db_file:
        uct_data_url = get_uct_data_url(target_sysinfo)
        uct_data = download_cache.get_data_from_url(uct_data_url)
    else:
        with open(opt.db_file) as db_file:
            uct_data = json.load(db_file)["data"]

    return uct_data


def get_uct_data_url(target_sysinfo):
    return const.UCT_DATA_URL % target_sysinfo.codename


@spin("Scanning for vulnerable packages...", "Scan complete!\n", "Scan failed!\n")
def run_scan(target_sysinfo, uct_data, logger):
    cve_scanner = CVEScanner(logger)

    return cve_scanner.scan(
        target_sysinfo.codename, uct_data, target_sysinfo.installed_pkgs
    )


def main():
    args = parse_args()

    try:
        opt = Options(args)
    except (ArgumentError, ValueError) as err:
        error_exit("Invalid option or argument: %s" % err, const.CLI_ERROR_RETURN_CODE)

    error_exit_code = (
        const.NAGIOS_UNKNOWN_RETURN_CODE if opt.nagios_mode else const.ERROR_RETURN_CODE
    )

    try:
        try:
            logger = set_output_verbosity(opt)
            local_sysinfo = LocalSysInfo(logger)
            target_sysinfo = TargetSysInfo(opt, local_sysinfo)

            debug.log_config_options(opt, logger)
            debug.log_local_system_info(local_sysinfo, opt.manifest_mode, logger)
            debug.log_target_system_info(target_sysinfo, logger)
        except (FileNotFoundError, PermissionError) as err:
            error_exit("Failed to determine the correct Ubuntu codename: %s" % err)
        except DistribIDError as di:
            error_exit(
                "Invalid linux distribution detected, CVEScan must be run on Ubuntu: %s"
                % di
            )
        except PkgCountError as pke:
            error_exit("Failed to determine the local package count: %s" % pke)

        download_cache = USTDownloadCache(logger)
        uct_data = load_uct_data(opt, download_cache, target_sysinfo)

        scan_results = run_scan(target_sysinfo, uct_data, logger)

        output_formatter = load_output_formatter(opt, logger)
        (formatted_output, return_code) = output_formatter.format_output(
            scan_results, target_sysinfo
        )

        output_logger = get_output_logger(opt, logger)
        output(output_logger, formatted_output, return_code)
        sys.exit(return_code)
    except socket.gaierror as se:
        error_exit(
            f"Failed to send syslog output to {opt.syslog_host}:{opt.syslog_port} -- {se}",
            error_exit_code,
        )
    except Exception as ex:
        error_exit(
            "An unexpected error occurred while running CVEScan: %s" % ex,
            error_exit_code,
        )


def output(output_logger, formatted_output, return_code):
    if return_code == const.SUCCESS_RETURN_CODE:
        output_logger.info(formatted_output)
    else:
        output_logger.warning(formatted_output)


def get_output_logger(opt, logger):
    if opt.syslog or opt.syslog_light:
        return get_syslog_logger(opt.syslog_host, opt.syslog_port)

    return logger


def get_syslog_logger(host, port):
    class _ContextFilter(logging.Filter):
        def __init__(self):
            self.hostname = socket.gethostname()

        def filter(self, record):
            record.hostname = self.hostname
            return True

    formatter = logging.Formatter(
        "%(hostname)s - cvescan - %(levelname)s - %(message)s"
    )
    syslog_handler = logging.handlers.SysLogHandler(
        address=(host, port), socktype=socket.SOCK_DGRAM
    )
    syslog_handler.setFormatter(formatter)

    syslog_logger = logging.getLogger(const.SYSLOG_LOGGER_NAME)
    syslog_logger.addFilter(_ContextFilter())
    syslog_logger.addHandler(syslog_handler)
    syslog_logger.setLevel(logging.INFO)

    return syslog_logger


if __name__ == "__main__":
    main()
