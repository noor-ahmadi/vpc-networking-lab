CREATE TABLE demo_message (id integer PRIMARY KEY, message text NOT NULL);
INSERT INTO demo_message VALUES (1, 'Hello from the isolated subnet');
GRANT SELECT ON demo_message TO vpc_app;
