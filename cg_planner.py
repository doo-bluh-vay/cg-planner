#!/usr/bin/env python3

# Capital Gains Planner
# Author: Balaji Sundaresan
# Estimate capital gains and tax for mutual fund redemptions 
# from a consolidated account statement.

import argparse
import getpass
import json
import logging
import subprocess
import sys
import casparser
import casparser_isin
import sqlite3
import os

from datetime import datetime
from functools import wraps
from pathlib import Path

import pandas as pd
import requests
from colorama import Fore, init
from tabulate import tabulate

init()

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def log_function_call(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        logger.debug("Entering %s", func.__qualname__)
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.exception("Exception in %s", func.__qualname__)
            raise
        finally:
            logger.debug("Exiting %s", func.__qualname__)

    return wrapper


# Constants
VERSION = "1.0.0"
GRANDFATHERED_DATE = datetime(2018, 1, 31)
STAMP_DUTY_DATE = datetime(2020, 7, 1)
STAMP_DUTY_RATE = 0.00005
GF_NAV_FILENAME = "gf_nav_31Jan2018.txt"
GF_NAV_FILE = Path(__file__).resolve().parent / "config" / GF_NAV_FILENAME
ISIN_FILE = "./isin.db"
NAV_API_URL = "https://api.mfapi.in/mf/{amfi_code}/latest"  
NAV_API_TIMEOUT = 20  # seconds
START_OF_FISCAL_YEAR_MONTH = 4  # April
ISO_8601_DATE_FORMAT = "%Y-%m-%d"
AMFI_CODE_TABLE_WIDTH = 20
SCHEME_NAME_TABLE_WIDTH = 80
SCHEME_TYPE_TABLE_WIDTH = 20
CESS = 4.0  # Cess rate in percentage
EQUITY_LTCG_EXEMPTION = 125000
GOLD_LTCG = 12.5
EQUITY_LTCG = 12.5
EQUITY_STCG = 20
CAPITAL_GAINS_PERIOD_1YEAR = 365
CAPITAL_GAINS_PERIOD_2YEARS = 730

os.environ["CASPARSER_ISIN_DB"] = ISIN_FILE

@log_function_call
def get_financial_start_year(date_obj: datetime) -> str:
    if date_obj.month < START_OF_FISCAL_YEAR_MONTH:
        return date_obj.year - 1
    return date_obj.year

@log_function_call
def get_financial_end_year(date_obj: datetime) -> str:
    if date_obj.month < START_OF_FISCAL_YEAR_MONTH:
        return date_obj.year
    return date_obj.year + 1

ADV_TAX_DUE_1 = datetime(get_financial_start_year(datetime.now()), 6, 15)
ADV_TAX_DUE_2 = datetime(get_financial_start_year(datetime.now()), 9, 15)
ADV_TAX_DUE_3 = datetime(get_financial_start_year(datetime.now()), 12, 15)
ADV_TAX_DUE_4 = datetime(get_financial_end_year(datetime.now()), 3, 15)

@log_function_call
def get_latest_nav_for_scheme(amfi_code: str) -> float:
    print(Fore.YELLOW + f"Fetching from URL = {NAV_API_URL.format(amfi_code=amfi_code)}")
    try:
        response = requests.get(NAV_API_URL.format(amfi_code=amfi_code), timeout=NAV_API_TIMEOUT)
        response.raise_for_status()
        response_json = response.json()
    except (requests.RequestException, ValueError) as error:
        raise RuntimeError(f"Unable to fetch latest NAV for scheme {amfi_code}: {error}") from error

    nav_data = response_json.get("data", [])
    if not nav_data:
        raise RuntimeError(f"Latest NAV not found for scheme {amfi_code}")

    nav = float(nav_data[0]["nav"])
    print(Fore.GREEN + f"INFO: Latest NAV for scheme {amfi_code} = {nav}")
    return nav


@log_function_call
def get_grandfathered_nav(amfi_code: str) -> float:
    # 1. Connect to the database file (creates it if it doesn't exist)
    conn = sqlite3.connect(ISIN_FILE)

    try:
        # 2. Create a cursor object to execute commands
        cursor = conn.cursor()
        
        # 3. Execute your SQL query
        cursor.execute("select nav from nav20180131,scheme where scheme.isin=nav20180131.isin and scheme.amfi_code='" + amfi_code + "'")
        
        # 4. Fetch the results
        row = cursor.fetchone()
        return float(row[0])

    except sqlite3.Error as e:
        raise RuntimeError(f"Unable to read grandfathered NAV not found for scheme {amfi_code}")

    finally:
        # 5. Always close the connection when done
        conn.close()    


@log_function_call
def is_float(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


@log_function_call
def parse_statement(pdf_file: Path, password: str) -> dict:
    if not pdf_file.is_file():
        raise FileNotFoundError(f"PDF file does not exist: {pdf_file}")

    json_str = casparser.read_cas_pdf(str(pdf_file), password, output="json")
    return json.loads(json_str)
            

@log_function_call
def get_schemes(statement: dict) -> list[list[str]]:
    schemes = []
    for folio in statement["folios"]:
        for scheme in folio["schemes"]:
            if "GOLD" in scheme["scheme"].upper():
                schemes.append([str("GOLD"), scheme["scheme"], str(scheme["amfi"])])
            else:
                schemes.append([str(scheme["type"]), scheme["scheme"], str(scheme["amfi"])])

            previous_date = None
            for transaction in scheme["transactions"]:
                transaction_date = datetime.strptime(transaction["date"], ISO_8601_DATE_FORMAT)
                if previous_date is not None and transaction_date < previous_date:
                    raise ValueError(
                        "Transactions are not in sorted order for scheme "
                        f"{scheme['scheme']}"
                    )
                previous_date = transaction_date
    schemes.sort(key=lambda x: (x[0], x[1]))
    return schemes


@log_function_call
def print_schemes(statement: dict) -> None:
    schemes = get_schemes(statement)
    logger.info(
        "Scheme listing:\n%s",
        tabulate(
            schemes,
            headers=["SCHEME TYPE", "SCHEME NAME", "AMFI CODE"],
            tablefmt="grid",
            maxcolwidths=[SCHEME_TYPE_TABLE_WIDTH, SCHEME_NAME_TABLE_WIDTH, AMFI_CODE_TABLE_WIDTH],
        )
    )


@log_function_call
def get_lots(statement: dict) -> dict[str, list[dict]]:
    lots_by_amfi_code: dict[str, list[dict]] = {}

    for folio in statement["folios"]:
        for scheme in folio["schemes"]:
            amfi_code = str(scheme["amfi"])
            lots = lots_by_amfi_code.setdefault(amfi_code, [])
            for transaction in scheme["transactions"]:
                transaction_type = transaction["type"]
                transaction_date = datetime.strptime(transaction["date"], ISO_8601_DATE_FORMAT)

                if transaction_type in {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN"}:
                    transaction_units = float(transaction["units"])
                    lots.append(
                        {
                            "purchase_date": transaction_date,
                            "purchase_nav": float(transaction["nav"]),
                            "purchase_amount": float(transaction["amount"]),
                            "remaining_units": transaction_units,
                        }
                    )
                elif transaction_type == "REVERSAL":
                    if not lots:
                        raise ValueError(
                            f"REVERSAL has no matching purchase in scheme {scheme['scheme']}"
                        )
                    last_lot = lots[-1]
                    matches_last_purchase = (
                        last_lot["purchase_date"] == transaction_date
                        and last_lot["purchase_nav"] == float(transaction["nav"])
                        and last_lot["remaining_units"] == abs(transaction_units)
                        and last_lot["purchase_amount"] == abs(float(transaction["amount"]))
                    )
                    if not matches_last_purchase:
                        raise ValueError(
                            "REVERSAL transaction does not match the last purchase for "
                            f"scheme {scheme['scheme']}"
                        )
                    lots.pop()
                elif transaction_type in {"REDEMPTION", "SWITCH_OUT"}:
                    transaction_units = float(transaction["units"])
                    units_to_redeem = abs(transaction_units)
                    for lot in lots:
                        units_from_lot = min(lot["remaining_units"], units_to_redeem)
                        lot["remaining_units"] -= units_from_lot
                        units_to_redeem -= units_from_lot
                        if units_to_redeem <= 0:
                            break
                    if units_to_redeem > 0.0005:
                        raise ValueError(
                            f"Statement redeems more units than available for scheme {scheme['scheme']}"
                        )
            print(Fore.GREEN + f"INFO: Scheme {scheme['scheme']} (AMFI code {amfi_code}) has {len(lots)} lots")
            

    return lots_by_amfi_code


@log_function_call
def calculate_redemption(
    statement: dict, scheme_to_redeem: str, amount_to_redeem: float, tax_slab: float, surcharge: float, cess: float
) -> tuple[pd.DataFrame, float, float]:
    
    schemes = get_schemes(statement)  # Validate schemes and transactions

    lots_by_amfi_code = get_lots(statement)
    if scheme_to_redeem not in lots_by_amfi_code:
        raise ValueError(
            f"AMFI code {scheme_to_redeem} is not an scheme in the statement"
        )

    for scheme in schemes:
        if scheme[2] == scheme_to_redeem:
            asset_type = scheme[0]
            break
    print(Fore.GREEN + f"INFO: Scheme {scheme_to_redeem} is of type {asset_type}")

    latest_nav = get_latest_nav_for_scheme(scheme_to_redeem)
    if latest_nav <= 0:
        raise ValueError(f"Latest NAV for scheme {scheme_to_redeem} must be positive")

    units_to_redeem = amount_to_redeem / latest_nav
    available_units = sum(
        max(0.0, lot["remaining_units"])
        for lot in lots_by_amfi_code[scheme_to_redeem]
    )
    if units_to_redeem > available_units + 0.0005:
        raise ValueError(
            f"Requested redemption is {units_to_redeem:.3f} units, but only "
            f"{available_units:.3f} units are available"
        )

    if asset_type == "EQUITY":
        #grandfathered_navs = fetch_grandfathered_nav()
        #grandfathered_nav = grandfathered_navs.get(scheme_to_redeem, 0.0)
        grandfathered_nav = get_grandfathered_nav(scheme_to_redeem)
    else:
        grandfathered_nav = 0.0
    today = datetime.now()
    gains_rows = []
    remaining_to_redeem = units_to_redeem
    total_capital_gains = 0.0
    total_tax = 0.0
    available_equity_ltcg_exemption = EQUITY_LTCG_EXEMPTION

    for lot in lots_by_amfi_code[scheme_to_redeem]:
        available_from_lot = max(0.0, lot["remaining_units"])
        if available_from_lot <= 0 or remaining_to_redeem <= 0:
            continue

        sold_units = min(available_from_lot, remaining_to_redeem)
        considered_nav = lot["purchase_nav"]
        if asset_type == "EQUITY":
            if lot["purchase_date"] <= GRANDFATHERED_DATE and grandfathered_nav:
                considered_nav = grandfathered_nav

        purchase_amount = sold_units * considered_nav
        if lot["purchase_date"] >= STAMP_DUTY_DATE:
            purchase_amount += purchase_amount * STAMP_DUTY_RATE
        sell_amount = sold_units * latest_nav
        capital_gains = sell_amount - purchase_amount
        if asset_type == "EQUITY":

            if (today - lot["purchase_date"]).days > CAPITAL_GAINS_PERIOD_1YEAR:
                capital_gains_term = "long-term"
                #txn_tax = "12.5% (plus cess and surcharge)"
                if ( capital_gains <= available_equity_ltcg_exemption ):
                    txn_tax = 0.0
                    available_equity_ltcg_exemption -= capital_gains
                elif ( capital_gains > available_equity_ltcg_exemption ):
                    txn_tax = compute_tax((capital_gains - available_equity_ltcg_exemption), EQUITY_LTCG, surcharge, cess)        
                    available_equity_ltcg_exemption = 0.0
                #print(str(capital_gains) + " " + str(available_equity_ltcg_exemption))
            else:
                capital_gains_term = "short-term"
                #txn_tax = "20% (plus cess and surcharge)"
                txn_tax = compute_tax(capital_gains, EQUITY_STCG, surcharge, cess)    
        elif asset_type == "DEBT":
            capital_gains_term = "all-short-term-only"
            #txn_tax = "Tax Slab Rates (plus cess and surcharge)"
            txn_tax = compute_tax(capital_gains, tax_slab, surcharge, cess)
        elif asset_type == "GOLD":
            if (today - lot["purchase_date"]).days > CAPITAL_GAINS_PERIOD_2YEARS:
                capital_gains_term = "long-term"
                #txn_tax = "12.5% (plus cess and surcharge)" 
                txn_tax = compute_tax(capital_gains, GOLD_LTCG, surcharge, cess)
            else:
                capital_gains_term = "short-term"
                #txn_tax = "Tax Slab Rates (plus cess and surcharge)"
                txn_tax = compute_tax(capital_gains, tax_slab, surcharge, cess)
        total_tax += txn_tax
        gains_rows.append(
            [
                lot["purchase_date"],
                lot["purchase_nav"],
                grandfathered_nav,
                considered_nav,
                sold_units,
                purchase_amount,
                sell_amount,
                capital_gains,
                capital_gains_term,
                txn_tax
            ]
        )
        total_capital_gains += capital_gains
        remaining_to_redeem -= sold_units

    columns = [
        "purchase_date",
        "purchase_nav",
        "grandfathered_nav",
        "considered_nav",
        "sell_units",
        "purchase_amount",
        "sell_amount",
        "capital_gains",
        "term",
        "txn_tax"
    ]
    return pd.DataFrame(gains_rows, columns=columns), total_capital_gains, total_tax

@log_function_call
def compute_tax(capital_gains: float, tax_slab: float, surcharge: float, cess: float) -> float:
    tax = capital_gains * (tax_slab/100)
    total_tax = tax + (tax * (surcharge / 100)) + (tax * (cess / 100))
    return total_tax

@log_function_call
def compute_advance_tax_due(capital_gains: float) -> list[(str, float)]:
    if capital_gains <= 10000:
        return [("No advance tax due (Pay by 31st March)", capital_gains)]
    else:
            if (datetime.now() <= ADV_TAX_DUE_1):
                return [("15th June", capital_gains * 0.15),("15th September", capital_gains * 0.30),("15th December", capital_gains * 0.30),("15th March", capital_gains * 0.25)]
            elif  (datetime.now() <= ADV_TAX_DUE_2):
                return [("15th September", capital_gains * 0.45),("15th December", capital_gains * 0.30),("15th March", capital_gains * 0.25)]
            elif  (datetime.now() <= ADV_TAX_DUE_3):
                return [("15th December", capital_gains * 0.75),("15th March", capital_gains * 0.25)]
            elif  (datetime.now() <= ADV_TAX_DUE_4):
                return [("15th March", capital_gains * 1)]

@log_function_call
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Estimate redemption capital gains.",
        epilog=(
            "Examples:\n"
            "  python capital_gains.py help\n"
            "  python capital_gains.py version\n"
            '  python capital_gains.py list --pdf "statement.pdf"\n'
            '  python capital_gains.py redeem --pdf "statement.pdf" '
            '--scheme 103490 --amount 225000 --tax-slab 12.5 --surcharge 15\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    modes = parser.add_subparsers(dest="mode", required=True)

    help_parser = modes.add_parser("help", help="Explain all modes and parameters.")
    help_parser.set_defaults(action="help")

    version_parser = modes.add_parser("version", help="Provides the version of this executable.")
    version_parser.set_defaults(action="version")

    list_parser = modes.add_parser(
        "list", help="List scheme names and AMFI codes from a password-protected CAS PDF."
    )
    add_statement_arguments(list_parser)
    list_parser.set_defaults(action="list")

    redeem_parser = modes.add_parser(
        "redeem",
        help="Estimate capital gains for redeeming an amount from an equity scheme.",
    )
    add_statement_arguments(redeem_parser)
    redeem_parser.add_argument(
        "--scheme",
        required=True,
        help="AMFI code of the equity scheme to redeem.",
    )
    redeem_parser.add_argument(
        "--amount",
        required=True,
        type=float,
        help="Amount to redeem in rupees; must be greater than zero.",
    )
    redeem_parser.add_argument(
        "--tax-slab",
        required=True,
        type=float,
        help="Tax slab rate in percentage.",
    )
    redeem_parser.add_argument(
        "--surcharge",
        required=True,
        type=float,
        help="Surcharge rate in percentage.",
    )
    redeem_parser.set_defaults(action="redeem")
    return parser


@log_function_call
def add_statement_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--pdf",
        required=True,
        type=Path,
        help="Path to the consolidated account statement PDF.",
    )
    parser.add_argument(
        "--password",
        help="PDF password. If omitted, it will be requested without echoing.",
    )


def print_banner() -> None:
    print(
        f"Capital Gains Planner v{VERSION}\n"
        "Author: Balaji Sundaresan\n"
        "Estimate capital gains and tax for mutual fund redemptions "
        "from consolidated account statements.\n"
    )


@log_function_call
def main(argv: list[str] | None = None) -> int:
    print_banner()
    parser = build_parser()
    args = parser.parse_args(argv)
    logger.info(
        "Command line parameters: mode=%s, pdf=%s, scheme=%s, amount=%s",
        args.mode,
        getattr(args, "pdf", None),
        getattr(args, "scheme", None),
        getattr(args, "amount", None),
    )

    if args.action == "help":
        parser.print_help()
        print(
            "\nModes:\n"
            "  help    Show this help and explain all parameters.\n"
            "  list    Require --pdf; accept --password or prompt for it; "
            "list scheme names and AMFI codes.\n"
            "  redeem  Require --pdf, --scheme, and --amount; accept --password "
            "or prompt for it; estimate capital gains.\n\n"
            "Parameters:\n"
            "  --pdf PATH       Required PDF statement path for list and redeem.\n"
            "  --password TEXT  Optional PDF password. If omitted, the program "
            "prompts without echoing the password.\n"
            "  --scheme CODE    Required AMFI code of an equity scheme for redeem.\n"
            "  --amount RUPEES  Required positive redemption amount for redeem."
        )
        return 0

    if args.action == "version":
        print("Version = " + VERSION)
        return 0

    if args.action == "redeem" and args.amount <= 0:
        parser.error("--amount must be greater than zero")

    password = args.password
    if password is None:
        password = getpass.getpass("PDF password: ")

    try:
        statement = parse_statement(args.pdf, password)
        if args.action == "list":
            print_schemes(statement)
        else:
            gains, total_capital_gains, total_tax = calculate_redemption(
                statement, args.scheme, args.amount, args.tax_slab, args.surcharge, CESS
            )
            adv_tax_payment_schedule = compute_advance_tax_due(total_tax)
            logger.info("Capital Gains Summary:")
            gains_display = gains.copy()
            gains_display['purchase_date'] = gains_display['purchase_date'].dt.strftime('%Y-%m-%d')
            logger.info(tabulate(gains_display, headers='keys', tablefmt='grid', showindex=False))
            logger.info("Total Capital Gains: %s", total_capital_gains)
            logger.info("Estimated Tax: %s", total_tax)
            logger.info("Estimated Advance Tax : %s", adv_tax_payment_schedule )
    except (FileNotFoundError, OSError, subprocess.CalledProcessError, ValueError, RuntimeError) as error:
        print(Fore.RED + f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
