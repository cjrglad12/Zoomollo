"""Where a company's people are: in the US or abroad.

Nothing public gives headcount by country (that's LinkedIn's insight data), so
Zoomollo judges it from what it can see:

- where its open roles are: if 20% or more of the roles with a stated location
  are outside the US (and at least 3 roles have one), most likely 20%+ of the
  staff are too;
- its own website: "offshore development center", "global delivery center",
  "nearshore team", "our team in India / the Philippines / ..." and the like.

Either one marks the company a bad prospect (score 0, off Today).
"""

from __future__ import annotations

import re

ABROAD_SHARE = 0.20
MIN_LOCATED = 3

_STATES = ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|"
           "Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|"
           "Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|"
           "North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|"
           "South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|"
           "District of Columbia|Puerto Rico")
_ABBR = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|"
         "OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC|PR")
_US = re.compile(rf"\b(?:united states|u\.s\.a?\.?|usa|us[- ]remote|remote[- ,(]+(?:us|usa|united states)|{_STATES})\b"
                 rf"|,\s*(?:{_ABBR})\b(?:\s*,?\s*(?:us|usa|\d{{5}}))?\s*$|^\s*(?:{_ABBR})\s*,\s*(?:us|usa)\b", re.I)

_COUNTRIES = (
    "india|philippines|pakistan|bangladesh|sri lanka|nepal|vietnam|viet nam|indonesia|malaysia|thailand|china|"
    "taiwan|hong kong|singapore|japan|korea|australia|new zealand|canada|mexico|guatemala|costa rica|"
    "el salvador|honduras|nicaragua|panama|colombia|peru|chile|argentina|brazil|uruguay|ecuador|bolivia|"
    "venezuela|dominican republic|jamaica|united kingdom|england|scotland|wales|ireland|france|germany|"
    "spain|portugal|italy|netherlands|belgium|switzerland|austria|sweden|norway|denmark|finland|poland|"
    "czech republic|czechia|slovakia|hungary|romania|bulgaria|serbia|croatia|bosnia|ukraine|belarus|"
    "moldova|lithuania|latvia|estonia|greece|turkey|türkiye|israel|egypt|morocco|tunisia|nigeria|kenya|"
    "ghana|south africa|uae|united arab emirates|saudi arabia|qatar|kuwait|bahrain|oman|armenia|georgia \\(country\\)")
_CITIES = (
    "bangalore|bengaluru|hyderabad|pune|chennai|mumbai|new delhi|delhi|noida|gurgaon|gurugram|kolkata|ahmedabad|"
    "kochi|coimbatore|jaipur|chandigarh|indore|trivandrum|thiruvananthapuram|mohali|manila|makati|taguig|cebu|"
    "pasig|quezon city|davao|karachi|lahore|islamabad|dhaka|colombo|ho chi minh|hanoi|da nang|jakarta|"
    "kuala lumpur|bangkok|shanghai|beijing|shenzhen|guangzhou|toronto|vancouver|montreal|calgary|ottawa|"
    "mississauga|monterrey|guadalajara|mexico city|ciudad de mexico|tijuana|bogota|bogotá|medellin|medellín|"
    "lima|santiago|buenos aires|sao paulo|são paulo|montevideo|san jose, costa rica|london|manchester|"
    "edinburgh|glasgow|belfast|dublin|cork|paris|berlin|munich|frankfurt|hamburg|madrid|barcelona|lisbon|"
    "porto|amsterdam|rotterdam|brussels|zurich|geneva|vienna|stockholm|oslo|copenhagen|helsinki|warsaw|"
    "krakow|kraków|wroclaw|wrocław|gdansk|prague|brno|budapest|bucharest|cluj|iasi|sofia|belgrade|novi sad|"
    "zagreb|kyiv|kiev|lviv|kharkiv|minsk|vilnius|riga|tallinn|athens|istanbul|tel aviv|cairo|nairobi|lagos|"
    "accra|cape town|johannesburg|dubai|abu dhabi|riyadh|doha|yerevan|tbilisi|sydney|melbourne|brisbane|"
    "perth|auckland|singapore|tokyo|seoul|taipei")
# Foreign city names that are also US towns.
_US_TOO = {"paris", "london", "dublin", "athens", "berlin", "lima", "manchester", "cairo", "warsaw", "vienna",
           "melbourne", "glasgow", "cork", "belfast", "geneva", "perth", "sydney", "delhi", "santiago",
           "hamburg", "frankfurt", "oslo", "stockholm", "amsterdam", "madrid", "panama", "peru", "mexico",
           "lisbon", "sofia", "riga", "porto", "jakarta", "manila", "colombo", "kochi"}
_ABROAD = re.compile(rf"\b(?:{_COUNTRIES}|{_CITIES})\b", re.I)
_ABROAD_CODE = re.compile(r",\s*(?:IND|PHL|PAK|MEX|CAN|GBR|UK|POL|ROU|UKR|COL|ARG|BRA|CRI|DEU|FRA|ESP|PRT|IRL|NLD|"
                          r"VNM|CHN|EGY|ISR|ZAF|AUS|SGP)\s*$")


def where(location: str | None) -> str | None:
    """'us', 'abroad' or None (no location, or just 'Remote')."""
    loc = (location or "").strip()
    if not loc:
        return None
    hits = {m.group(0).lower() for m in _ABROAD.finditer(loc)}
    if hits or _ABROAD_CODE.search(loc):
        # "Paris, TX", "Dublin, OH" and "Melbourne, FL" are in the US.
        if hits and hits <= _US_TOO and re.search(rf",\s*(?:{_ABBR}|{_STATES})\b", loc, re.I):
            return "us"
        return "abroad"
    if _US.search(loc):
        return "us"
    return None


_OFFSHORE_TEXT = re.compile(
    rf"\b(?:offshore|near[- ]?shore)\s+(?:development|delivery|engineering|team|teams|center|centre|office|model|"
    rf"resources|talent|staff)"
    rf"|\bglobal\s+(?:delivery|development|capability|capabilities)\s+(?:center|centre|centers|centres)"
    rf"|\b(?:development|delivery|engineering|operations|support)\s+(?:center|centre)s?\s+in\s+(?:the\s+)?(?:{_COUNTRIES}|{_CITIES})\b"
    rf"|\b(?:our|a)\s+(?:team|teams|office|offices|engineers|staff)\s+in\s+(?:the\s+)?(?:{_COUNTRIES}|{_CITIES})\b", re.I)


def offshore_phrase(text: str | None) -> str | None:
    """A phrase on a company's own site that says much of the work is done abroad."""
    m = _OFFSHORE_TEXT.search(text or "")
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else None


def abroad_reason(job_locations: list[str | None], site_note: str | None = None) -> str | None:
    """Why this company likely has 20%+ of its people outside the US, or None."""
    places = [w for w in (where(x) for x in job_locations) if w]
    abroad = places.count("abroad")
    if len(places) >= MIN_LOCATED and abroad / len(places) >= ABROAD_SHARE:
        return f"{abroad} of {len(places)} open roles are outside the US (likely 20%+ of staff abroad)"
    if site_note:
        return f"much of their team is abroad (their site: “{site_note}”)"
    return None
