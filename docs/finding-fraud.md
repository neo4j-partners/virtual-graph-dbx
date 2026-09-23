# Finding fraud with the Virtual Graph

This walkthrough shows how the Finance Genie Virtual Graph finds money laundering. It
covers fraud queries 1 to 10, the same set `uv run vg-demo --demo fraud` runs. Every query
below has been tested on the Virtual Graph. Most come back in a few seconds. Query 7 takes
about 15 seconds. Query 10 is the slowest and takes about three and a half minutes. Copy
and paste each one as written.

## Overview of the Data

The data creates a graph of money moving between people and businesses.

- **Accounts** are people or businesses. Each one has a balance, the date it was
  opened, and the account holder's age.
- **A transfer** is one account sending money to another account. We write it as
  `TRANSFERRED_TO`. Every transfer records the amount and the time it happened.
- **Merchants** are shops and services. When an account pays a merchant we call it
  `TRANSACTED_WITH`, and it records the amount and the time.

Accounts send money to each other, and accounts pay merchants. Everything below is "follow the money" across that graph.

The dataset also carries a ground-truth label. The `account_labels` table marks 1,000 of
the 25,000 accounts as fraud, a 4% base rate. The Virtual Graph does not expose it, and
none of the queries use it. It is the answer key for checking how well each query works.

The transfer and purchase data ends on 30 March 2024, so the "recent activity" queries
use a fixed cutoff date near the end of the data instead of today's date. In a live
system you would work the cutoff out from the current date.

## How a launderer moves money

Money laundering is usually described in three stages. Each stage leaves a different
footprint in the data, and each query below is tuned to one footprint.

1. **Placement.** Get the dirty cash into the banking system without setting off
   alarms. Its footprints are lots of transfers sized just under the reporting limit and
   new accounts that start moving large sums.
2. **Layering.** Move the money around to blur where it came from. Its footprints are
   the same money bouncing back and forth between two accounts and accounts that push
   out far more money than they actually hold.
3. **Integration.** Bring the now-clean money back together and pull it out. Its
   footprints are collection accounts that gather money from many senders, one account
   spraying money out to many others, and accounts that behave like couriers.

The demo follows these three stages in order. Queries 8 to 10 then follow the money two
hops at a time and look for accounts acting together.

## The demo script

Paste each query into your Cypher console against the Virtual Graph and read the rows
that come back. Query 7 finishes with a quick matching step you do on the results. That
step is spelled out under the query in plain terms.

To run the same set from the command line, use `uv run vg-demo --demo fraud`. The demo
runs queries 1 to 10 with a 300-second timeout per query. The whole run takes about 4
minutes.

Every query sorts by the column that matters most and then by id, so rows that tie on
that column come back in the same order on every run. Queries 1 to 6 and 8 to 10 return
at most 50 rows each. Query 7 returns every account that passes its threshold.

### Stage 1: Placement

#### 1. Structuring: transfers kept just under the limit

Banks have to report large transfers, so launderers split a big sum into many
transfers sized just under the limit. Here the limit is $10,000, so the query looks at
transfers between $9,000 and $9,999. It ranks every account by how many such transfers
it sent. Runs in about a second.

```cypher
MATCH (src:Account)-[t:TRANSFERRED_TO]->(:Account)
WHERE t.amount >= 9000 AND t.amount < 10000
WITH src.account_id AS account_id, count(t) AS near_threshold,
     round(sum(t.amount), 2) AS total
RETURN account_id, near_threshold, total
ORDER BY near_threshold DESC, account_id ASC
LIMIT 50
```

Read it as: the accounts at the top sent the most transfers just under the limit. Each
row is one account:

* `account_id`: the account that sent the transfers.
* `near_threshold`: how many of its transfers landed between $9,000 and $9,999. A 2
  means it sent two such transfers.
* `total`: those transfers added up, in dollars.

Structuring is a faint signal in this sample. No account sent more than two such
transfers, and only four sent two. Most of the 50 rows are accounts with a single one.

#### 2. New accounts moving large sums

Real customers ramp up slowly. A recently opened account that pushes out large sums is a
red flag. This finds the accounts opened in the last 30 days of opening dates, 6 November
to 6 December 2022, and totals how much they sent. Runs in under a second.

```cypher
MATCH (a:Account)-[t:TRANSFERRED_TO]->(:Account)
WHERE a.opened_date >= date("2022-11-06")
WITH a.account_id AS account_id, a.opened_date AS opened_date,
     a.holder_age AS holder_age, count(t) AS transfers,
     round(sum(t.amount), 2) AS outflow
RETURN account_id, opened_date, holder_age, transfers, outflow
ORDER BY outflow DESC, account_id ASC
LIMIT 50
```

Read it as: the newest accounts near the top of this list already move money like
established accounts. Each row is one account:

* `account_id`: the account.
* `opened_date`: the day the account was opened.
* `holder_age`: the age of the person who owns it.
* `transfers`: how many transfers the account sent.
* `outflow`: the total dollars it sent out.

The account opening dates end in December 2022. The transfers all happen between January
and March 2024. These accounts are therefore about 13 to 17 months old when they transfer, so
the query finds the newest accounts in the data, each already over a year old.

### Stage 2: Layering

#### 3. Round trips between two accounts

Ordinary trade rarely sends the same money back and forth. Two accounts that keep paying
each other are likely "washing" money to create fake activity. This finds pairs of
accounts that send money in both directions and totals the money that moved between
them. Runs in three to four seconds.

```cypher
MATCH (a:Account)-[f:TRANSFERRED_TO]->(b:Account)-[g:TRANSFERRED_TO]->(a)
WHERE a.account_id < b.account_id
WITH a.account_id AS a_id, b.account_id AS b_id,
     count(DISTINCT f) AS n_ab, count(DISTINCT g) AS n_ba,
     sum(f.amount) AS sf, sum(g.amount) AS sg
RETURN a_id, b_id,
       round(sf / n_ba + sg / n_ab, 2) AS round_trip_volume,
       n_ab + n_ba                     AS leg_count
ORDER BY round_trip_volume DESC, a_id ASC, b_id ASC
LIMIT 50
```

Read it as: each row is a pair of accounts bouncing money between themselves. The
columns:

* `a_id` and `b_id`: the two accounts in the pair.
* `round_trip_volume`: all the money sent between them, both directions added together,
  in dollars.
* `leg_count`: how many transfers passed between them, counting both directions. A 4
  means four transfers.

The pattern matches once for every combination of one transfer each way. A pair with 4
transfers one way and 5 back produces 20 matches. The query therefore counts each
direction with `count(DISTINCT ...)` and divides each direction's sum by the other
direction's count. That gives the real transfer count and the real dollars.

The top pair is 7855 and 13727, with one transfer each way totaling $122,721.78. This is
one of the two strongest signals in the set. 93 of the 95 accounts in the top 50 pairs are
labeled fraud.

#### 4. Velocity ratio: accounts that move more than they hold

An account holding $1,000 that pushes $100,000 through is acting like a pipe. This
compares each account's total outflow to its current balance. Runs in
about a second.

```cypher
MATCH (a:Account)-[t:TRANSFERRED_TO]->(:Account)
WHERE a.balance > 0
WITH a.account_id AS account_id, a.balance AS balance, sum(t.amount) AS outflow
RETURN account_id,
       round(balance, 2)           AS balance,
       round(outflow, 2)           AS outflow_volume,
       round(outflow / balance, 1) AS velocity_ratio
ORDER BY velocity_ratio DESC, account_id ASC
LIMIT 50
```

Read it as: a high `velocity_ratio` means the account moved far more money than it
holds today. Treat it as one signal among several. Each row is one account:

* `account_id`: the account.
* `balance`: how much money it holds right now, in dollars.
* `outflow_volume`: the total it has sent out, in dollars.
* `velocity_ratio`: the outflow divided by the balance. A 287 means it moved about 287
  times its current balance.

### Stage 3: Integration

#### 5. Collection accounts: money piling in from many senders

A normal person is rarely paid by dozens of strangers in a single week. An account that
receives money from many different senders in a short window looks like a collection
point. This query counts the different senders paying into each account in the last week
of the data. It keeps the accounts with five or more. Runs in about a second.

```cypher
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
WITH dst.account_id AS recipient, count(DISTINCT src.account_id) AS senders,
     count(t) AS transfers, round(sum(t.amount), 2) AS inflow
WHERE senders >= 5
RETURN recipient AS account_id, senders, transfers, inflow
ORDER BY senders DESC, account_id ASC
LIMIT 50
```

Read it as: every row is a candidate collection account. Each row is one account:

* `account_id`: the account receiving the money, the one you are investigating.
* `senders`: how many different accounts paid into it that week.
* `transfers`: how many separate transfers it received that week.
* `inflow`: the total dollars it received that week.

#### 6. Spray accounts: one account paying out to many

The mirror image. One account splitting a big pile into many small transfers to many
different accounts is "smurfing." This query counts the different accounts each
account paid in the last week of the data. It keeps the accounts with five or more.
Runs in about a second.

```cypher
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
WITH src.account_id AS sender, count(DISTINCT dst.account_id) AS recipients,
     count(t) AS transfers, round(sum(t.amount), 2) AS outflow
WHERE recipients >= 5
RETURN sender AS account_id, recipients, transfers, outflow
ORDER BY recipients DESC, account_id ASC
LIMIT 50
```

Read it as: every row is a candidate spray account. Each row is one account:

* `account_id`: the account paying out, the one you are investigating.
* `recipients`: how many different accounts it paid that week.
* `transfers`: how many separate transfers it sent that week.
* `outflow`: the total dollars it sent that week.

These same two result sets also show the **hubs**. An account near the top of both lists
both gathers and distributes money, so it sits at the center of the network. In this
sample, 17 accounts appear in both top-50 lists, and none of them is labeled fraud. They
behave like payment aggregators. Treat a hub as a busy account to explain before you
treat it as a suspect.

#### 7. Courier accounts: lots of transfers, little shopping

A real customer both moves money to people and buys from shops. An account with heavy
peer-to-peer transfer activity and little merchant spend behaves like a courier. This
takes two queries. The first counts each account's transfers in
both directions. It keeps the accounts with 100 or more. The second counts each account's
merchant purchases. Together they run in about 15 seconds, and the first query takes
about 11 of them.

```cypher
MATCH (a:Account)-[tr:TRANSFERRED_TO]-(:Account)
WITH a.account_id AS account_id, count(tr) AS transfer_count
WHERE transfer_count >= 100
RETURN account_id, transfer_count
ORDER BY transfer_count DESC, account_id ASC
```

```cypher
MATCH (a:Account)-[tw:TRANSACTED_WITH]->(:Merchant)
WITH a.account_id AS acct, count(tw) AS merchant_count
RETURN acct AS account_id, merchant_count
```

Each row in the first result is one account:

* `account_id`: the account.
* `transfer_count`: how many transfers it took part in, sending or receiving, added
  together. This is its total peer-to-peer activity.

Each row in the second result is one account:

* `account_id`: the account.
* `merchant_count`: how many purchases it made from merchants. An account missing from
  this list made zero merchant purchases.

Then line the two results up by account. Keep each account from the first list that
has fewer than 20 merchant purchases in the second list. Count an account missing from
the second list as zero purchases. Every account that survives is acting like a money
courier.

In this sample the first list has 1,200 accounts, and 1,196 of them survive the match.
Every one of them made at least one merchant purchase. Purchase counts run from 0 to 24
across all accounts, and only one account made none. The most common counts are 9 and 10.
Counts of 7 to 12 cover 66% of accounts. The 100-transfer threshold is therefore what
does the work. This is the other strong signal in the set. 998 of the 1,196 survivors are
labeled fraud, a precision of 83%. Those 998 accounts are 99.8% of all fraud accounts, a
recall of 99.8%.

This matching step runs on your side because the Virtual Graph rejects `OPTIONAL
MATCH`, so one query cannot keep accounts with no purchases.

### Going deeper: two-hop relays and coordinated rings

The first seven queries look at one account or one pair at a time. The next three follow
money through a middle account, or look for many accounts acting together on the same day.
Queries 8 and 10 show the shape of money being relayed onward. In this sample their top
lists are high-volume hubs, and none of those accounts is labeled fraud. Use them to show
the relay pattern on accounts the earlier queries already flagged.

#### 8. Pass-through mule: money in, same money out

A mule account receives money and passes almost the same amount on to someone else. This
query finds every account that received a transfer and then sent one of nearly the same
size, within 5%, to a different account. The outgoing transfer can happen at the same time
as the incoming one or later. Runs in four to six seconds.

```cypher
MATCH (a:Account)-[t_in:TRANSFERRED_TO]->(mule:Account)
      -[t_out:TRANSFERRED_TO]->(b:Account)
WHERE t_out.transfer_timestamp >= t_in.transfer_timestamp
  AND abs(t_out.amount - t_in.amount) <= 0.05 * t_in.amount
  AND a <> b
WITH mule.account_id AS mule_id, t_in, count(*) AS outs
RETURN mule_id,
       sum(outs)                  AS passthroughs,
       count(t_in)                AS forwarded_in,
       round(sum(t_in.amount), 2) AS volume
ORDER BY passthroughs DESC, mule_id ASC
LIMIT 50
```

Read it as: the accounts at the top are the busiest relay points in the network. Each row
is one account:

* `mule_id`: the account in the middle, the one that received and forwarded the money.
* `passthroughs`: how many matching pairs of one incoming and one outgoing transfer it
  has. The outgoing transfer happens at the same time as the incoming one or later. One
  incoming transfer can match several outgoing ones.
* `forwarded_in`: how many different incoming transfers were forwarded at least once.
* `volume`: the dollars that came in on those forwarded transfers, each transfer counted
  once.

The first `WITH` groups by mule and incoming transfer, so each incoming transfer is
counted once in `forwarded_in` and `volume`. The top row is 13914, with 599 pass-throughs
built from 190 incoming transfers worth $46,634.25.

In this sample the query surfaces high-volume hubs. None of its top 50 accounts is labeled
fraud, and all 50 are already on the query 7 courier list. The query adds the relay shape
to the picture. It adds no precision beyond query 7.

The column is named `mule_id` on purpose. The query touches three
accounts. An `account_id` alias would be ambiguous in the SQL the Virtual Graph generates,
and the query would fail.

The textbook form of this query also requires the money to go out within 48 hours. The
Virtual Graph cannot add a duration to a timestamp inside `WHERE`, so this version keeps
only the "out after in" ordering.

#### 9. Shared-merchant burst: many accounts, one shop, one day

Fraud rings often test stolen cards or cash out at the same small merchant on the same
day. This query groups purchases by merchant and by day and collects the different
accounts that bought there. It keeps the days where four or more accounts showed up at a
merchant. Runs in about 5 seconds.

```cypher
MATCH (a:Account)-[t:TRANSACTED_WITH]->(m:Merchant)
WITH m, date(t.txn_timestamp) AS day,
     collect(DISTINCT a.account_id) AS accounts,
     count(t)                       AS txns
WHERE size(accounts) >= 4 AND txns <= 200
RETURN m.merchant_id AS merchant_id, m.merchant_name AS merchant_name, day,
       size(accounts) AS account_count, txns, accounts
ORDER BY account_count DESC, merchant_id ASC, day ASC
LIMIT 50
```

The `txns <= 200` cap is a safeguard against bulk merchant days, where a crowd is normal.
It never applies to this sample, because the busiest merchant day has 7 purchases.

Read it as: each row is one merchant on one day where a group of accounts converged. Look
for the same accounts showing up together across several rows. The columns:

* `merchant_id` and `merchant_name`: the merchant.
* `day`: the day of the burst.
* `account_count`: how many different accounts bought there that day.
* `txns`: how many purchases the merchant had that day in total.
* `accounts`: the list of account ids that bought there that day.

In this sample the 50 rows name 258 different accounts. Only 3 of them appear in more than
one row.

#### 10. Rapid-turnover summary per account: money forwarded over and over

A single pass-through can be innocent. An account that does it dozens of times is acting
like a pipe. This query counts, for each account in the middle, every pair of one transfer
in and one transfer out to a different account. The transfer out happens at the same time
as the transfer in or later. The query keeps the accounts with 50 or more such pairs and
reports the average time between receiving and forwarding.

This is the slowest query in the set. It joins every transfer into an account to every
transfer out of it at the same time or later. No time window or anchor narrows the join.
It takes about 210
seconds, or three and a half minutes. That fits inside the demo's 300-second timeout.

```cypher
MATCH (src:Account)-[t_in:TRANSFERRED_TO]->(mule:Account)
      -[t_out:TRANSFERRED_TO]->(dst:Account)
WHERE t_out.transfer_timestamp >= t_in.transfer_timestamp
  AND src <> dst
WITH mule,
     count(*) AS rapid_pairs,
     avg(duration.inSeconds(t_in.transfer_timestamp,
                            t_out.transfer_timestamp)) AS avg_turnaround
WHERE rapid_pairs >= 50
RETURN mule.account_id AS account_id, rapid_pairs, avg_turnaround
ORDER BY rapid_pairs DESC, account_id ASC
LIMIT 50
```

Read it as: a high `rapid_pairs` count means the account keeps forwarding what it receives.
A short turnaround makes the signal stronger. Each row is one account:

* `account_id`: the account in the middle.
* `rapid_pairs`: how many receive-then-forward pairs it took part in.
* `avg_turnaround`: the average time between receiving money and sending money on, as a
  Cypher Duration.

The query returns the turnaround as a Duration because `duration.inSeconds` measures the
gap between the two timestamps exactly. The demo converts the Duration to hours on the
client and prints it as `avg_turnaround_hours`. The values match the same calculation run
directly in Databricks SQL. Reading `.epochMillis` off the timestamps returns the same
values, but the server flags it with an `01N52` unknown-property warning.

The textbook form also limits each pair to 24 hours. The Virtual Graph cannot add a
duration to a timestamp inside `WHERE`, so this version averages over every
forward-after-receive pair. The word "rapid" in the name describes the intent. The query
itself enforces no time limit. In this sample
the top 50 accounts average 29 to 32 days between receiving and forwarding. The top row is
13914, with 33,229 pairs and an average of 756.8 hours.

Like query 8, this query surfaces high-volume hubs in this sample. None of its top 50
accounts is labeled fraud, and all 50 are already on the query 7 courier list. It shares 34
of its top 50 with query 8. It adds the turnover shape. It adds no precision beyond
query 7.

#### 11. Layering cycles: not supported on the Virtual Graph

Query 11 looks for money that travels in a loop of two to four hops back to where it
started. The Virtual Graph rejects its quantified path pattern with `42NG1`, so the demo
prints "Not run" for it. Run it against a loaded Aura graph instead. The cycles recipe in
[`best-practices.md`](../best-practices.md#adaptation-recipes) describes a workaround on
the Virtual Graph.

## Visualize the suspects

The queries above give you lists. The payoff of a graph is seeing the *shape*. Take any
account id a query flagged, paste it into one of the queries below, and run it in the
Aura Workspace Query tab to draw the picture. These return node and relationship
variables, so the Workspace renders them as a graph.

**Anchor on a specific account id.** A filter such as
`{account_id: 3375}` pushes a selective filter down to the warehouse, so the query stays
fast and returns few enough nodes to draw. The same pattern without an anchor scans the
whole table and returns an arbitrary slice of the network. The example ids below are real
accounts from the test data, and all of them are labeled fraud. Swap in your own.

The star queries also carry the same one-week window as queries 5 and 6. The picture then
shows the transfers the query counted. The window also keeps the result under the `LIMIT`.

#### See a collection account: the fan-in star

Anchor on a recipient flagged by demo query 5. This draws every account that paid into
it that week, fanned out around the center. Account 3375 below received 26 transfers from
20 different senders that week. The query returns all 26 in about a second.

```cypher
MATCH (sender:Account)-[t:TRANSFERRED_TO]->(a:Account {account_id: 3375})
WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
RETURN sender, t, a
LIMIT 50
```

To pick your own collection account, this finder returns the recipients with the most
different senders in the last week, in about a second. Use a `recipient` from it as the
anchor above.

```cypher
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
WITH dst.account_id AS recipient, count(DISTINCT src.account_id) AS senders
RETURN recipient, senders
ORDER BY senders DESC, recipient
LIMIT 5
```

The finder's top row is account 184, with 24 senders. Account 184 is one of the
legitimate hubs, so check a candidate against the other lists before you present it as a
suspect.

#### See a spray account: the fan-out star

Anchor on a sender flagged by demo query 6. This draws everyone it paid that week, fanned
out around the center: the mirror image of the collection star. Account 2599 below sent
19 transfers to 19 different accounts that week. The query returns all 19 in about a
second.

```cypher
MATCH (a:Account {account_id: 2599})-[t:TRANSFERRED_TO]->(recipient:Account)
WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
RETURN a, t, recipient
LIMIT 50
```

To pick your own spray account, this finder returns the senders with the most different
recipients in the last week.

```cypher
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
WITH src.account_id AS sender, count(DISTINCT dst.account_id) AS recipients
RETURN sender, recipients
ORDER BY recipients DESC, sender
LIMIT 5
```

The finder's top row is account 16570, with 21 recipients. Account 16570 is not labeled
fraud, so check a candidate against the other lists before you present it as a suspect.

Keep the `sender` alias. The query touches two accounts, so an `account_id` alias would
be ambiguous in the generated SQL and the query would fail.

#### See a round-trip pair: the wash edge

Anchor on the two ids from demo query 3. This draws the two accounts with the transfers
running between them in both directions, which is the wash shape in its simplest form.
The top pair, 7855 and 13727, returns its two transfers, one each way, in about a second.

```cypher
MATCH (a:Account {account_id: 7855})-[t:TRANSFERRED_TO]-(b:Account {account_id: 13727})
RETURN a, t, b
```

The basic examples add more warm-up and visualization queries. They cover ego networks
around merchants, two accounts linked through a shared merchant, and transfer chains. See
[`basic-graph-examples.md`](../basic-graph-examples.md). The same anchoring rule applies
to all of them.

## How it all ties together

Each query scans all 25,000 accounts on its own. The stages give the queries an order to
run in. The payoff comes from reading the results side by side and checking them against
the fraud labels. The table shows the share of each top list that is labeled fraud,
against a 4% base rate.

| Query | What it flags | Labeled fraud |
|---|---|---|
| 1 | Structuring | 22 of 50, 44% |
| 2 | New accounts moving large sums | 15 of 50, 30% |
| 3 | Round trips between two accounts | 93 of the 95 accounts in 50 pairs, 98% |
| 4 | Velocity ratio | 24 of 50, 48% |
| 5 | Collection accounts | 1 of 50, 2% |
| 6 | Spray accounts | 7 of 50, 14% |
| 7 | Courier accounts | 998 of 1,196, 83% |
| 8 | Pass-through mule | 0 of 50, 0% |
| 9 | Shared-merchant burst | 88 of the 258 accounts in 50 rows, 34% |
| 10 | Rapid-turnover summary per account | 0 of 50, 0% |

Read the table in three groups.

1. **Queries 3 and 7 are the strongest signals.** Almost every round-trip pair is fraud
   on both ends, and the courier list catches nearly every fraud account in the data.
2. **Queries 1, 2, 4 and 9 are supporting signals.** Each one flags fraud at 7 to 12 times
   the base rate. An account that also appears on query 3 or 7 is worth a closer look.
3. **Queries 5, 6, 8 and 10 mostly find busy legitimate accounts.** The 17 hubs that top
   both queries 5 and 6 are all legitimate. Query 6 still flags fraud at 14%, about 3.5
   times the base rate. The spray example 2599 comes from its list. Queries 8 and 10
   surface high-volume hubs in this sample. None of their top 50 accounts is labeled
   fraud, and every one of them is already on the query 7 list. They add pattern shape to
   the story. They add no precision. In this sample the hubs on these lists behave like
   payment aggregators. The second caveat below covers them.

A single suspicious number is easy to explain away. An account that lands on a strong
list and a supporting list, and whose picture has the expected shape, is the story that
holds up.

## Candidates, not verdicts

These queries surface candidates. Three caveats apply before you call any of them fraud:

- **Validate against ground truth.** The dataset includes the held-out
  `account_labels.is_fraud` table. Measure the precision of any rule against it
  before trusting it, as the table above does. Check its recall too. Query 7 reports a
  recall of 99.8%. Say "clearly fraud" only after you have
  measured it.
- **Legitimate accounts share the fingerprint.** Payment aggregators, payroll
  processors, marketplace settlement accounts, and P2P-app float accounts show the same
  high fan-in, fan-out and turnover. The hubs and the top relay accounts in this sample
  behave like these accounts. The hard part is separating these accounts from fraud.
- **Watch confounded metrics.** The velocity ratio in query 4 divides by the current
  balance, so a small balance alone pushes an account up the list. The top 50 accounts
  hold a median balance of $3,776, against $247,686 across all accounts. Combine it with
  other signals.
