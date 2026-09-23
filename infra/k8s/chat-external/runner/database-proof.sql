-- Root supplies psql -v run_id=... -v phase=... against the existing dedicated lab.
-- Read-only. No message text, token or password is selected.
SELECT coalesce(json_agg(row_to_json(proof)), '[]'::json)
FROM (
    SELECT m.id::text AS message_id,
           m.conversation_id::text AS conversation_id,
           m.connection_id::text AS connection_id,
           m.external_message_id,
           c.profile,
           ec.external_conversation_id,
           p.external_sender_id,
           encode(sha256(convert_to(m.text, 'UTF8')), 'hex') AS text_sha256,
           m.seq::text AS seq
      FROM chat.external_messages AS m
      JOIN chat.external_connections AS c ON c.id = m.connection_id
      JOIN chat.external_conversations AS ec ON ec.id = m.conversation_id AND ec.connection_id = m.connection_id
      LEFT JOIN chat.external_participants AS p ON p.id = m.participant_id AND p.connection_id = m.connection_id
     WHERE c.run_id = :'run_id'
       AND starts_with(m.external_message_id, :'phase' || '-message-')
     ORDER BY m.connection_id, m.external_message_id, m.seq
) AS proof;
