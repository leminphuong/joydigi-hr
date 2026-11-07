-- JOYOKR read-only export through a database tool (phpMyAdmin / Adminer),
-- for hosts without SSH or WP-CLI. Same data as joyokr_export.php.
--
-- * SELECT only: nothing in WordPress is changed, no folder is created.
-- * Replace every `wp_` below with the site's real table prefix
--   ($table_prefix in wp-config.php; phpMyAdmin shows it on every table name).
-- * Run each SELECT on its own and export the RESULT (not the table) as CSV:
--   UTF-8, comma separated, values enclosed by ", NULL written as NULL,
--   "Put columns names in the first row" ticked. Save it under the file name
--   written above the query. Never export wp_users or wp_usermeta whole: they
--   hold password hashes and session tokens.
-- * Then on the rehearsal machine:
--   manage.py okr_import_wp assemble --from-dir <csv folder> --uploads-dir <files copy> \
--       --exported-at <UTC time of the export, e.g. 2026-11-07T08:00:00Z> --out joyokr-export.json

-- departments.csv
SELECT * FROM wp_joyokr_departments ORDER BY id;

-- teams.csv
SELECT * FROM wp_joyokr_teams ORDER BY id;

-- team_members.csv
SELECT * FROM wp_joyokr_team_members ORDER BY team_id, user_id;

-- cycles.csv
SELECT * FROM wp_joyokr_cycles ORDER BY id;

-- objectives.csv
SELECT * FROM wp_joyokr_objectives ORDER BY id;

-- key_results.csv
SELECT * FROM wp_joyokr_key_results ORDER BY id;

-- checkins.csv
SELECT * FROM wp_joyokr_checkins ORDER BY id;

-- comments.csv
SELECT * FROM wp_joyokr_comments ORDER BY id;

-- evidence.csv
SELECT * FROM wp_joyokr_evidence ORDER BY id;

-- notifications.csv
SELECT * FROM wp_joyokr_notifications ORDER BY id;

-- activity_log.csv
SELECT * FROM wp_joyokr_activity_log ORDER BY id;

-- users.csv  (identity columns only)
SELECT ID, user_login, user_email, display_name, user_registered FROM wp_users ORDER BY ID;

-- usermeta.csv  (roles and the four JOYOKR profile keys only)
SELECT user_id, meta_key, meta_value FROM wp_usermeta
WHERE meta_key IN ('wp_capabilities', 'joyokr_employee_code', 'joyokr_department_id',
                   'joyokr_manager_id', 'joyokr_employment_status')
ORDER BY user_id, meta_key;

-- attachments.csv  (only the media items that OKR evidence points at)
SELECT p.ID, p.post_mime_type, m.meta_value AS attached_file
FROM wp_posts p
LEFT JOIN wp_postmeta m ON m.post_id = p.ID AND m.meta_key = '_wp_attached_file'
WHERE p.ID IN (SELECT DISTINCT attachment_id FROM wp_joyokr_evidence WHERE attachment_id > 0)
ORDER BY p.ID;

-- options.csv  (non-secret settings only; no Telegram token, no SSO secret)
SELECT option_name, option_value FROM wp_options
WHERE option_name IN ('siteurl', 'home', 'timezone_string', 'gmt_offset', 'stylesheet', 'upload_path',
                      'joyokr_schema_version', 'joyokr_warning_threshold', 'joyokr_reminder_frequency',
                      'joyokr_email_reminders', 'joyokr_rating_ranges')
ORDER BY option_name;

-- evidence-files.csv  (the list of files to copy out of wp-content/uploads, keeping the folders)
SELECT DISTINCT m.meta_value AS relative_path
FROM wp_joyokr_evidence e
JOIN wp_postmeta m ON m.post_id = e.attachment_id AND m.meta_key = '_wp_attached_file'
WHERE e.attachment_id > 0
ORDER BY 1;
