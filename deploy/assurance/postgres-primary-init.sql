-- SPDX-License-Identifier: Apache-2.0
\getenv replication_password REPLICATION_PASSWORD
CREATE ROLE nomos_replication WITH REPLICATION LOGIN PASSWORD :'replication_password';
SELECT pg_create_physical_replication_slot('assurance_standby');
