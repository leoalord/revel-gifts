-- Runs once on first initialization of the Postgres volume.
-- OpenFGA uses its own logical database, separate from the application database `revel`.
CREATE DATABASE openfga OWNER revel;
