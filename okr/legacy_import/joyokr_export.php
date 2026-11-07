<?php
/**
 * Read-only JOYOKR export for the Django OKR migration (format version 1).
 *
 * Run on a COPY of the WordPress site, or on the live site by its
 * administrator, with WP-CLI:
 *
 *     wp eval-file joyokr_export.php > joyokr-export.json
 *
 * It only runs SELECT queries and reads files' sizes and checksums. It
 * never reads or writes user_pass, user_activation_key, session tokens,
 * the Telegram bot token, Telegram chat ids, phone numbers or the SSO
 * secret. Evidence files themselves are not copied: give the importer a
 * copy of wp-content/uploads (--uploads-dir) instead.
 */

defined( 'ABSPATH' ) || exit( "Run with: wp eval-file joyokr_export.php\n" );

// PHP notices must never end up inside the JSON written to STDOUT.
@ini_set( 'display_errors', 'stderr' );

global $wpdb;

$joyokr_tables = array(
	'departments'   => 'id',
	'teams'         => 'id',
	'team_members'  => 'team_id, user_id',
	'cycles'        => 'id',
	'objectives'    => 'id',
	'key_results'   => 'id',
	'checkins'      => 'id',
	'comments'      => 'id',
	'evidence'      => 'id',
	'notifications' => 'id',
	'activity_log'  => 'id',
);

$export = array(
	'format'         => 'joyokr-export',
	'format_version' => 1,
	'source'         => array(
		'site_url'        => home_url( '/' ),
		'theme_version'   => (string) wp_get_theme()->get( 'Version' ),
		'schema_version'  => (string) get_option( 'joyokr_schema_version', '' ),
		'table_prefix'    => $wpdb->prefix,
		'exported_at'     => gmdate( 'Y-m-d\TH:i:s\Z' ),
		'timezone_string' => (string) get_option( 'timezone_string', '' ),
		'gmt_offset'      => (string) get_option( 'gmt_offset', '' ),
		'uploads_basedir' => wp_upload_dir( null, false )['basedir'],
	),
	// Only non-secret settings. The Telegram token and SSO values stay out.
	'options'        => array(
		'warning_threshold'  => get_option( 'joyokr_warning_threshold', '' ),
		'reminder_frequency' => get_option( 'joyokr_reminder_frequency', '' ),
		'email_reminders'    => get_option( 'joyokr_email_reminders', '' ),
		'rating_ranges'      => get_option( 'joyokr_rating_ranges', '' ),
	),
	'users'          => array(),
	'tables'         => array(),
	'attachments'    => array(),
);

$found_tables = array();
foreach ( $joyokr_tables as $name => $order ) {
	$table = $wpdb->prefix . 'joyokr_' . $name;
	$exists = $wpdb->get_var( $wpdb->prepare( 'SHOW TABLES LIKE %s', $table ) );
	$found_tables[ $name ] = (bool) $exists;
	$export['tables'][ $name ] = $exists ? $wpdb->get_results( "SELECT * FROM `{$table}` ORDER BY {$order}", ARRAY_A ) : array();
}

// Users: identity fields only. Never user_pass / user_activation_key.
$users = $wpdb->get_results( "SELECT ID, user_login, user_email, display_name, user_registered FROM {$wpdb->users} ORDER BY ID", ARRAY_A );
foreach ( $users as $user ) {
	$id   = (int) $user['ID'];
	$data = get_userdata( $id );
	$export['users'][] = array(
		'id'                => $id,
		'login'             => $user['user_login'],
		'email'             => $user['user_email'],
		'display_name'      => $user['display_name'],
		'registered'        => $user['user_registered'],
		'roles'             => $data ? array_values( (array) $data->roles ) : array(),
		'employee_code'     => (string) get_user_meta( $id, 'joyokr_employee_code', true ),
		'department_id'     => (string) get_user_meta( $id, 'joyokr_department_id', true ),
		'manager_id'        => (string) get_user_meta( $id, 'joyokr_manager_id', true ),
		'employment_status' => (string) get_user_meta( $id, 'joyokr_employment_status', true ),
	);
}

// Evidence attachments: where the file is, its size and SHA-256.
$basedir = trailingslashit( wp_upload_dir( null, false )['basedir'] );
foreach ( $export['tables']['evidence'] as $row ) {
	$attachment_id = (int) ( $row['attachment_id'] ?? 0 );
	if ( $attachment_id <= 0 || isset( $export['attachments'][ $attachment_id ] ) ) {
		continue;
	}
	$path     = get_attached_file( $attachment_id );
	$relative = ( $path && 0 === strpos( $path, $basedir ) ) ? substr( $path, strlen( $basedir ) ) : '';
	$readable = $path && is_readable( $path );
	$export['attachments'][ $attachment_id ] = array(
		'id'            => $attachment_id,
		'relative_path' => $relative,
		'mime_type'     => (string) get_post_mime_type( $attachment_id ),
		'original_name' => $path ? wp_basename( $path ) : '',
		'size'          => $readable ? filesize( $path ) : null,
		'sha256'        => $readable ? hash_file( 'sha256', $path ) : '',
		'missing'       => ! $readable,
	);
}
$export['attachments'] = array_values( $export['attachments'] );

// Self-check, written into the file and printed on STDERR: row counts, files
// the site no longer has, links that point at nothing. Nothing is corrected
// here; the importer refuses broken links (okr/legacy_import/validate.py).
$ids = array();
foreach ( $export['tables'] as $name => $rows ) {
	$ids[ $name ] = array();
	foreach ( $rows as $row ) {
		if ( isset( $row['id'] ) ) {
			$ids[ $name ][ (int) $row['id'] ] = true;
		}
	}
}
$user_ids = array();
foreach ( $export['users'] as $user ) {
	$user_ids[ (int) $user['id'] ] = true;
}
$broken = array();
$check  = function ( $label, $table, $column, $target, $optional = true ) use ( $export, $ids, &$broken ) {
	$broken[ $label ] = 0;
	foreach ( $export['tables'][ $table ] as $row ) {
		$value = (int) ( $row[ $column ] ?? 0 );
		if ( $value <= 0 && $optional ) {
			continue;
		}
		if ( ! isset( $ids[ $target ][ $value ] ) ) {
			$broken[ $label ]++;
		}
	}
};
$check( 'objectives.cycle_id', 'objectives', 'cycle_id', 'cycles', false );
$check( 'objectives.parent_objective_id', 'objectives', 'parent_objective_id', 'objectives' );
$check( 'key_results.objective_id', 'key_results', 'objective_id', 'objectives', false );
$check( 'checkins.key_result_id', 'checkins', 'key_result_id', 'key_results', false );
$check( 'evidence.key_result_id', 'evidence', 'key_result_id', 'key_results', false );
$check( 'evidence.checkin_id', 'evidence', 'checkin_id', 'checkins' );
$check( 'cycles.parent_id', 'cycles', 'parent_id', 'cycles' );
$check( 'team_members.team_id', 'team_members', 'team_id', 'teams', false );
$broken['comments.object_id'] = 0;
foreach ( $export['tables']['comments'] as $row ) {
	$target = 'key_result' === ( $row['object_type'] ?? '' ) ? 'key_results' : 'objectives';
	if ( ! isset( $ids[ $target ][ (int) ( $row['object_id'] ?? 0 ) ] ) ) {
		$broken['comments.object_id']++;
	}
}
$broken['owner_user_id (objectives + key_results)'] = 0;
foreach ( array( 'objectives', 'key_results' ) as $table ) {
	foreach ( $export['tables'][ $table ] as $row ) {
		if ( ! isset( $user_ids[ (int) ( $row['owner_user_id'] ?? 0 ) ] ) ) {
			$broken['owner_user_id (objectives + key_results)']++;
		}
	}
}
$missing_files = 0;
foreach ( $export['attachments'] as $item ) {
	if ( $item['missing'] ) {
		$missing_files++;
	}
}
$export['summary'] = array(
	'tables_found'  => count( array_filter( $found_tables ) ),
	'counts'        => array_map( 'count', $export['tables'] ),
	'users'         => count( $export['users'] ),
	'attachments'   => count( $export['attachments'] ),
	'missing_files' => $missing_files,
	'broken_links'  => $broken,
);
fwrite( STDERR, sprintf( "JOYOKR export: %d/11 tables, %d users, %d evidence files (%d missing)\n",
	$export['summary']['tables_found'], $export['summary']['users'], $export['summary']['attachments'], $missing_files ) );
foreach ( $export['summary']['counts'] as $name => $count ) {
	fwrite( STDERR, sprintf( "  %-14s %d rows\n", $name, $count ) );
}
foreach ( $broken as $label => $count ) {
	if ( $count ) {
		fwrite( STDERR, sprintf( "  BROKEN %s: %d\n", $label, $count ) );
	}
}

$json = wp_json_encode( $export, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT );
if ( false === $json ) {
	// Never hand over an empty or partial file as if it were the export.
	fwrite( STDERR, 'EXPORT FAILED: ' . json_last_error_msg() . "\n" );
	exit( 1 );
}
echo $json, "\n";
