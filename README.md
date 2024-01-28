# Geovelo for home-assistant

Component to expose information stored in a geovelo account to home assistant.

## Installation

It must be used as a custom repository via hacs.

## Configuration

Once the custom integration has been added, add "geovelo" integration through the UI. It will request a username, a password and a user id.
The user id can be found in the following way:
- open https://geovelo.app and connect using your credentials
- open your browser development tools
- go to https://geovelo.app/fr/user/stats/?p=01-2024
- in browser development tools, network tab,  search for "traces" call

The user id is part of the url: `https://backend.geovelo.fr/api/v5/users/<user_id>/traces?period=custom&date_start=01-01-2024&date_end=31-01-2024&ordering=-start_datetime&page=1`.

When clicking OK, the import of your data will start, it may take a while (60s for 1500 trips in my case).
