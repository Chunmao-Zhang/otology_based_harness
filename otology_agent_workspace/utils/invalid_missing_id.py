from typing import List


class Company:  # entity_type: Organization
    name: str
    operates_in_industry: List["Industry"]


class Industry:  # entity_type: BusinessDomain
    _id: str
    name: str
