from typing import List


class Company:
    name: str
    operates_in_industry: List["Industry"]


class Industry:
    _id: str
    name: str
