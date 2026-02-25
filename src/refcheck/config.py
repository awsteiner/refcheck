"""API key management via environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from CWD (where the MCP server is launched), then fallback to repo root
load_dotenv(Path.cwd() / ".env")
load_dotenv(Path(__file__).parent.parent.parent / ".env")


@dataclass
class Settings:
    crossref_email: str | None = None
    s2_api_key: str | None = None
    elsevier_key: str | None = None
    elsevier_insttoken: str | None = None
    ieee_api_key: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            crossref_email=os.getenv("CROSSREF_EMAIL"),
            s2_api_key=os.getenv("S2_API_KEY"),
            elsevier_key=os.getenv("ELSEVIER_KEY"),
            elsevier_insttoken=os.getenv("ELSEVIER_INSTTOKEN"),
            ieee_api_key=os.getenv("IEEE_API_KEY"),
        )

    def available_databases(self) -> list[str]:
        dbs = ["crossref", "arxiv"]  # always available
        dbs.append("semantic_scholar")  # works without key, better with key
        if self.elsevier_key:
            dbs.append("scopus")
        if self.ieee_api_key:
            dbs.append("ieee")
        return dbs
