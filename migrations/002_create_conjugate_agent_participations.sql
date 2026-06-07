CREATE TABLE IF NOT EXISTS conjugate_agent_participations (
    id INT NOT NULL AUTO_INCREMENT,
    room_id VARCHAR(64) NOT NULL,
    conjugate_agent_id INT NOT NULL,
    seat_number INT NOT NULL,
    role VARCHAR(32) NOT NULL,
    result VARCHAR(32) NOT NULL,
    is_winner BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_conjugate_agent_participation (room_id, conjugate_agent_id, seat_number),
    KEY idx_conjugate_agent_participations_agent_created (conjugate_agent_id, created_at),
    CONSTRAINT fk_conjugate_agent_participations_agent
        FOREIGN KEY (conjugate_agent_id) REFERENCES conjugate_agents(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
