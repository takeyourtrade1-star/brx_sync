-- Schema del catalogo MySQL verificato il 2026-09-07. Solo database di test.
CREATE TABLE `cards` (
  `oracle_id` char(36) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'UUID di Scryfall per il concetto della carta (lega tutte le stampe)',
  `name` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Nome inglese/oracle della carta',
  `cmc` float NOT NULL DEFAULT '0' COMMENT 'Costo di mana convertito',
  `color_identity` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL COMMENT 'Identità colore (es. ["W", "U"])',
  `colors` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin COMMENT 'Colori della carta (es. ["W"])',
  `keywords` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL COMMENT 'Array di keyword (es. ["Flying", "Deathtouch"])',
  `type_line` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Linea di tipo Oracle (es. Creature — Human Soldier)',
  `legalities` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL COMMENT 'JSON con lo stato in ogni formato (es. {"standard": "legal", "modern": "banned"})',
  PRIMARY KEY (`oracle_id`),
  KEY `idx_card_name` (`name`),
  FULLTEXT KEY `ft_card_conceptual` (`name`,`type_line`),
  CONSTRAINT `cards_chk_1` CHECK (json_valid(`color_identity`)),
  CONSTRAINT `cards_chk_2` CHECK (json_valid(`colors`)),
  CONSTRAINT `cards_chk_3` CHECK (json_valid(`keywords`)),
  CONSTRAINT `cards_chk_4` CHECK (json_valid(`legalities`))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `sets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `cardtrader_id` int DEFAULT NULL,
  `code` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `release_date` date DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `game_id` int NOT NULL DEFAULT '1',
  PRIMARY KEY (`id`),
  UNIQUE KEY `cardtrader_id` (`cardtrader_id`)
) ENGINE=InnoDB AUTO_INCREMENT=1963 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `cards_prints` (
  `id` int NOT NULL AUTO_INCREMENT,
  `oracle_id` char(36) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `base_card_id` int NOT NULL,
  `set_id` int NOT NULL,
  `cardtrader_id` int DEFAULT NULL,
  `scryfall_id` char(36) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `collector_number` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `rarity` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `condition_default` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT 'NM',
  `image_path` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `image_status` enum('pending','ok','rejected') COLLATE utf8mb4_unicode_ci DEFAULT 'pending',
  `available_languages` text COLLATE utf8mb4_unicode_ci COMMENT 'JSON array of available languages (e.g. ["en","it","fr"])',
  `has_foil` tinyint(1) DEFAULT '0' COMMENT '1 if foil version is available',
  `has_signed` tinyint(1) DEFAULT '0' COMMENT '1 if signed version is available',
  `has_altered` tinyint(1) DEFAULT '0' COMMENT '1 if altered version is available',
  `condition_options` text COLLATE utf8mb4_unicode_ci COMMENT 'JSON array of available conditions',
  PRIMARY KEY (`id`),
  UNIQUE KEY `cardtrader_id` (`cardtrader_id`),
  KEY `base_card_id` (`base_card_id`),
  KEY `set_id` (`set_id`),
  KEY `idx_oracle_id` (`oracle_id`),
  CONSTRAINT `cards_prints_ibfk_2` FOREIGN KEY (`set_id`) REFERENCES `sets` (`id`),
  CONSTRAINT `fk_cards_prints_oracle_id` FOREIGN KEY (`oracle_id`) REFERENCES `cards` (`oracle_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=99543 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
