"""Money, quantity and unit helpers.

Rules of the house:
  * money is integer paise, never float;
  * quantities are integer base units (g, ml, piece, packet);
  * all intermediate arithmetic uses Decimal with ROUND_HALF_UP at the paisa.
"""
from decimal import Decimal, ROUND_HALF_UP

ONE = Decimal(1)


def D(x) -> Decimal:
    """Coerce to Decimal (floats go via str so 10.5 stays 10.5)."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x).strip())


def rnd(x) -> int:
    """Round a Decimal to an integer, half-up."""
    return int(D(x).quantize(ONE, rounding=ROUND_HALF_UP))


def rupees_to_paise(x) -> int:
    return rnd(D(x) * 100)


def paise_to_rupees(p) -> Decimal:
    return (D(p) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _indian_group(int_part: str) -> str:
    if len(int_part) <= 3:
        return int_part
    head, tail = int_part[:-3], int_part[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def inr_plain(paise) -> str:
    """12345678 paise -> '1,23,456.78' (Indian digit grouping)."""
    r = paise_to_rupees(paise)
    sign = "-" if r < 0 else ""
    whole, frac = f"{abs(r):.2f}".split(".")
    return f"{sign}{_indian_group(whole)}.{frac}"


def inr(paise) -> str:
    return "₹" + inr_plain(paise)


# ---- units -------------------------------------------------------------
_FACTORS = {
    "g": {"kg": 1000, "g": 1},
    "ml": {"l": 1000, "ml": 1},
    "piece": {"piece": 1, "pc": 1, "pcs": 1},
    "packet": {"packet": 1, "pkt": 1},
}


def unit_factor(item, unit: str) -> int:
    unit = (unit or item["disp_unit"]).strip().lower()
    if unit == "carton" and item["base_unit"] == "packet":
        if not item["carton_size"]:
            raise ValueError(f"{item['name']} has no carton size defined")
        return int(item["carton_size"])
    try:
        return _FACTORS[item["base_unit"]][unit]
    except KeyError:
        raise ValueError(f"Cannot use unit '{unit}' for {item['name']} (base unit {item['base_unit']})")


def units_for(item) -> list:
    base = item["base_unit"]
    if base == "g":
        return ["kg", "g"]
    if base == "ml":
        return ["L", "ml"]
    if base == "packet":
        return ["packet"] + (["carton"] if item["carton_size"] else [])
    return ["piece"]


def to_base(qty, unit, item) -> int:
    """Convert a user quantity in `unit` to integer base units. Rejects sub-unit fractions."""
    q = D(qty) * unit_factor(item, unit)
    if q != q.to_integral_value():
        raise ValueError(f"{qty} {unit} is finer than 1 {item['base_unit']} for {item['name']}")
    return int(q)


def from_base(base, item) -> Decimal:
    """Integer base units -> Decimal in the item's display unit."""
    return D(base) / D(item["factor"])


def fmt_dec(d: Decimal) -> str:
    d = D(d)
    s = format(d.normalize(), "f")
    return s


def fmt_qty(base, item, with_unit=True) -> str:
    s = fmt_dec(from_base(base, item))
    return f"{s} {item['disp_unit']}" if with_unit else s


# ---- GST ---------------------------------------------------------------
def line_tax(taxable_paise: int, rate_bp: int, intra: bool):
    """Return (cgst, sgst, igst) in paise. CGST = SGST = half the rate, each rounded half-up."""
    if intra:
        half = rnd(D(taxable_paise) * rate_bp / 20000)
        return half, half, 0
    return 0, 0, rnd(D(taxable_paise) * rate_bp / 10000)


def fy_of(date_iso: str) -> str:
    y, m = int(date_iso[:4]), int(date_iso[5:7])
    start = y if m >= 4 else y - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


# ---- amount in words (Indian numbering) -------------------------------
_ONES = ("Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen "
         "Fifteen Sixteen Seventeen Eighteen Nineteen").split()
_TENS = "Zero Ten Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()


def _below_1000(n):
    out = []
    if n >= 100:
        out.append(_ONES[n // 100] + " Hundred")
        n %= 100
    if n >= 20:
        out.append(_TENS[n // 10] + ((" " + _ONES[n % 10]) if n % 10 else ""))
    elif n > 0:
        out.append(_ONES[n])
    return " ".join(out)


def words_inr(paise: int) -> str:
    rupees, ps = divmod(int(paise), 100)
    if rupees == 0 and ps == 0:
        return "Zero Rupees Only"
    parts = []
    for div, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand")):
        if rupees >= div:
            parts.append(_below_1000(rupees // div) + " " + name)
            rupees %= div
    if rupees:
        parts.append(_below_1000(rupees))
    text = " ".join(parts) + " Rupees" if parts else ""
    if ps:
        text += (" and " if text else "") + _below_1000(ps) + " Paise"
    return text + " Only"


STATES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra & Nagar Haveli and Daman & Diu", "27": "Maharashtra", "28": "Andhra Pradesh (old)",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu",
    "34": "Puducherry", "35": "Andaman & Nicobar", "36": "Telangana", "37": "Andhra Pradesh",
    "38": "Ladakh",
}


def state_label(code) -> str:
    return f"{code}-{STATES.get(code, '?')}" if code else ""
