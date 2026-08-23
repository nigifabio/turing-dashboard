#!/usr/bin/env python3
# One-time Spotify OAuth setup. Run with no args to get the authorize URL;
# after visiting it and approving, run again with the full redirect URL you
# land on as the argument to exchange it for a cached token.

import json
import sys
from pathlib import Path

from spotipy.oauth2 import SpotifyOAuth

REPO_DIR = Path(__file__).resolve().parent
CONFIG_PATH = REPO_DIR / "sonos_dashboard_config.json"
CACHE_PATH = REPO_DIR / ".spotify-cache"

with open(CONFIG_PATH) as f:
    sp_cfg = json.load(f)["spotify"]

oauth = SpotifyOAuth(
    client_id=sp_cfg["client_id"],
    client_secret=sp_cfg["client_secret"],
    redirect_uri=sp_cfg["redirect_uri"],
    scope="user-read-playback-state user-read-currently-playing",
    cache_path=str(CACHE_PATH),
    open_browser=False,
)

if len(sys.argv) > 1:
    redirect_response = sys.argv[1]
    code = oauth.parse_response_code(redirect_response)
    oauth.get_access_token(code, as_dict=False, check_cache=False)
    print("SUCCESS: token cached at", CACHE_PATH)
else:
    print(oauth.get_authorize_url())
