"""
GreenTech Hub press monitor — daily Allgäuer/Memminger Zeitung e-paper scan
for GreenTech Hub / portfolio-startup / partner-company mentions.

Independent of the VC-scouting pipeline (no shared Postgres/Qdrant state);
shares only the local Ollama summarizer and, per the owner's own
instruction, the existing newsletter Gmail account's sending capability.
See run_daily.py for the entry point and README.md for setup.
"""
