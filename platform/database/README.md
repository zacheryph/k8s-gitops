# Database Management

Initial creation of roles & databases is currently manual.

```sql
-- create the role and add to secrets
CREATE ROLE name WITH LOGIN
ENCRYPTED PASSWORD 'password';

-- create the database
CREATE DATABASE name WITH OWNER 'owner'
```
