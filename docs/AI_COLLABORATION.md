# AI collaboration log


## 1. Prompts

```text

ROLE
You are a senior full-stack engineer working with me on a take-home assessment.

INPUTS
- Requirement: fsd-take-home-assessment/problem_statement.md
- Data:        fsd-take-home-assessment/data/crm_events.json      (Source A - CRM)
               fsd-take-home-assessment/data/calendar_events.json (Source B - Calendar)

GOAL

## 1. Analyse the data 
  - Read every record in both files.
  - Build an expected-answer table: which CRM record pairs with which calendar
    record, which are single-source, and which are duplicates.
  - Find and list concrete examples of each problem the brief mentions:
      * no shared ID between sources
      * meetings in both sources vs only one
      * timestamps that don't match exactly (check timezones and daylight saving)
      * conflicting fields (location, format, status, time)
      * a duplicate within one source (don't confuse it with a recurring series)
      * missing or malformed fields (dates, times, emails, empty lists, placeholders)
  - Separate real conflicts from harmless wording differences
    (e.g. "Conference Room B" vs "HQ - Conference Room B").

```

## 2. Requirement breakdown

The brief asks for three things: **ingest** two sources, **reconcile** records that describe the same meeting, and **serve** a unified list with visible provenance and conflicts. 


## 3. Data analysis

We read all 42 records by hand **before** writing code and built the expected answer as a table: 17 cross-source pairs, 3 CRM-only, 4 calendar-only, and 1 calendar duplicate. The implementation was then checked against that table, not the other way round. Notable findings:

- **Timezone trap.** `CAL-A4` is the only record with `Z`. Converting 19:00Z with the correct DST offset (EDT, UTC-4, since DST began 9 Mar 2025) gives 15:00, while CRM says 14:00. The naive UTC-5 assumption would hide the conflict. We kept the correct conversion and surface the conflict.
- **Duplicate vs. recurrence.** `CAL-A5`/`CAL-A6` are one meeting: same day, 30 min apart, same client domain, and A6 was created later and adds the CIO, whom CRM said "may" attend. `CAL-A3`/`CAL-A18` look identical but are weekly occurrences. That is why same local date is a hard rule for duplicates.
- **Conflicts.** Location/format (`CRM-1002`), status (`CRM-1009` cancelled), time (`CRM-1004`, `CRM-1016`), plus many harmless wording differences that must *not* be flagged as conflicts ("Conference Room B" vs "HQ - Conference Room B").
- **Malformed data.** `"03-15/2025"`, a null time, `raj.patel[at]...`, `"external-guests"`, an end time with no seconds, an empty attendee list, and the placeholder client `"Multiple"`.