CREATE TABLE IF NOT EXISTS conjugate_agents (
    id INT NOT NULL AUTO_INCREMENT,
    fingerprint VARCHAR(128) NOT NULL,
    agent_name VARCHAR(128) NOT NULL,
    avatar_seed VARCHAR(128) NOT NULL,
    born_at DATETIME NOT NULL,
    skill_versions_json JSON NOT NULL,
    changelog TEXT NOT NULL,
    lore TEXT NOT NULL,
    games_played INT NOT NULL DEFAULT 0,
    wins INT NOT NULL DEFAULT 0,
    win_rate FLOAT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_conjugate_agents_fingerprint (fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
