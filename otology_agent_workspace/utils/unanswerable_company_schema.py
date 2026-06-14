from typing import List


class Company:
    _id: str
    name: str
    operates_in_industry: List["Industry"]


class Industry:
    _id: str
    name: str
