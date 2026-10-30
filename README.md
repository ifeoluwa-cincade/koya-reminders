# koya-reminders

A single GitHub Actions job that posts follow-up reminders to Discord for the Koya Outbound dashboard.
It holds no lead data and no credentials. Webhook links and the activity-log address are stored as repository secrets.

## Phone numbers: sources and the primary

Every phone number on a lead keeps a list of ALL the sources that found it (`phone_sources` JSON book on the Attio person,
version 2). The most reliable source decides the primary; a primary a person chose in Koya HQ is never overridden.
Ranking, best first (the same list lives in koya-app `lib/phones.js` RANK; change both together):

typed by a person > Prospeo (verified) > their email signature > ContactOut > AI Ark > SalesQL > Prospeo (unverified) >
website > imported list > synced from CloudTalk / unknown.

Details and the rules: `crm/scripts/phone_pick.py`.
