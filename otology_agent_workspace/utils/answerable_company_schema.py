from typing import List, Optional


class Company:
    _id: str
    name: str
    country: Optional[str]
    operates_in_industry: List["Industry"]


class Industry:
    _id: str
    name: str
