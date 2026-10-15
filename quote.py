"""Query Tigerair's booking site for exact tax-inclusive round-trip prices.

The booking site blocks headless browsers, so callers must launch a headed
Chrome (on Linux, run under xvfb-run).
"""
from playwright.sync_api import Page

ORIGIN, DEST, ADULTS, CHILDREN = "KHH", "KIX", 2, 1
URL = ("https://booking.tigerairtw.com/?outbound={o}-{d}&departureDate={dep}&inbound={d}-{o}"
       "&returnDate={ret}&adult={a}&children={c}&type=roundTrip&language=zh-TW&currencyCode=TWD")


class Blocked(Exception):
    """The booking site answered with Akamai's Access Denied page."""


def cheapest_fares(journey, seats):
    """Cheapest sellable fare per flight in a journey, cheapest flight first."""
    flights = []
    for leg in journey["legs"]:
        for al in leg["availabilityLegs"]:
            seg = al["availabilitySegments"][0]
            ok = [f for f in al["fares"] if f["sellable"] and f["availableCount"] >= seats]
            if not ok:
                continue
            f = min(ok, key=lambda f: f["paxFares"][0]["ticketPrice"]["discountedTotalAmount"])
            px = {p["paxType"]: p["ticketPrice"] for p in f["paxFares"]}
            flights.append({
                "flight": f'{seg["carrierCode"]}{seg["flightNumber"].strip()}',
                "dep": seg["departureTime"], "arr": seg["arrivalTime"],
                "productClass": f["productClass"], "seatsLeft": f["availableCount"],
                "adt": px["ADT"], "chd": px.get("CHD", px["ADT"]),
            })
    return sorted(flights, key=lambda x: x["adt"]["discountedTotalAmount"])


def quote(page: Page, dep, ret):
    result = {}
    def on_resp(r):
        if "graphql" in r.url and r.request.post_data and "appFlightSearchResult" in r.request.post_data:
            try:
                result["data"] = r.json()
            except Exception:
                pass
    page.on("response", on_resp)
    page.goto(URL.format(o=ORIGIN, d=DEST, dep=dep, ret=ret, a=ADULTS, c=CHILDREN),
              wait_until="domcontentloaded", timeout=60000)
    try:
        for _ in range(60):
            if "data" in result:
                break
            if "Access Denied" in page.title():
                raise Blocked(page.url)
            page.wait_for_timeout(1000)
    finally:
        page.remove_listener("response", on_resp)
    if "data" not in result:
        return {"dep": dep, "ret": ret, "error": f"no fare data (url={page.url})"}

    journeys = result["data"]["data"]["appFlightSearchResult"]["journeys"]
    out_f, ret_f = (cheapest_fares(j, ADULTS + CHILDREN) for j in journeys)
    if not out_f or not ret_f:
        return {"dep": dep, "ret": ret, "error": "no sellable fare for all passengers"}
    o, r = out_f[0], ret_f[0]
    adt = o["adt"]["discountedTotalAmount"] + r["adt"]["discountedTotalAmount"]
    chd = o["chd"]["discountedTotalAmount"] + r["chd"]["discountedTotalAmount"]
    return {
        "dep": dep, "ret": ret, "outbound": o, "return": r, "estimated": False,
        "fare_per_adult": o["adt"]["discountedFareAmount"] + r["adt"]["discountedFareAmount"],
        "tax_per_adult": o["adt"]["taxAmount"] + r["adt"]["taxAmount"],
        "total_per_adult": adt, "total_per_child": chd,
        "total_all": adt * ADULTS + chd * CHILDREN,
    }
