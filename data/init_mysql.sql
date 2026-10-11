-- My Agentix MySQL schema initialization
-- Creates legal knowledge base tables with UTF-8 support.

CREATE DATABASE IF NOT EXISTS legal_db
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE legal_db;
SET NAMES utf8mb4;

-- Drop tables for idempotent initialization during container bootstrap.
DROP TABLE IF EXISTS law_authorities;
DROP TABLE IF EXISTS article_interpretations;
DROP TABLE IF EXISTS article_citations;
DROP TABLE IF EXISTS article_causes;
DROP TABLE IF EXISTS interpretations;
DROP TABLE IF EXISTS articles;
DROP TABLE IF EXISTS chapters;
DROP TABLE IF EXISTS causes;
DROP TABLE IF EXISTS authorities;
DROP TABLE IF EXISTS laws;

CREATE TABLE authorities (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    level VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_authority_level (level)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

CREATE TABLE laws (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    effective_date DATE,
    doc_number VARCHAR(255),
    level VARCHAR(100),
    upper_law_id INT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE INDEX idx_law_name (name),
    INDEX idx_law_level (level),
    CONSTRAINT fk_laws_upper
        FOREIGN KEY (upper_law_id) REFERENCES laws(id)
        ON DELETE SET NULL
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

CREATE TABLE chapters (
    id INT AUTO_INCREMENT PRIMARY KEY,
    law_id INT NOT NULL,
    parent_id INT,
    title VARCHAR(255) NOT NULL,
    level VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_chapters_law (law_id),
    INDEX idx_chapters_parent (parent_id),
    CONSTRAINT fk_chapters_law
        FOREIGN KEY (law_id) REFERENCES laws(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_chapters_parent
        FOREIGN KEY (parent_id) REFERENCES chapters(id)
        ON DELETE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

CREATE TABLE articles (
    id INT AUTO_INCREMENT PRIMARY KEY,
    law_id INT NOT NULL,
    chapter_id INT,
    article_no VARCHAR(50) NOT NULL,
    content TEXT NOT NULL,
    keywords VARCHAR(500),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY unique_article (law_id, article_no),
    INDEX idx_articles_law (law_id),
    INDEX idx_articles_chapter (chapter_id),
    INDEX idx_articles_keywords (keywords),
    CONSTRAINT fk_articles_law
        FOREIGN KEY (law_id) REFERENCES laws(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_articles_chapter
        FOREIGN KEY (chapter_id) REFERENCES chapters(id)
        ON DELETE SET NULL
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

CREATE TABLE article_citations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    article_id INT NOT NULL,
    cited_article_id INT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_citation (article_id, cited_article_id),
    INDEX idx_citations_article (article_id),
    INDEX idx_citations_cited (cited_article_id),
    CONSTRAINT fk_citations_article
        FOREIGN KEY (article_id) REFERENCES articles(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_citations_cited
        FOREIGN KEY (cited_article_id) REFERENCES articles(id)
        ON DELETE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

CREATE TABLE causes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    category ENUM('民事', '刑事', '行政') DEFAULT '民事',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_cause_category (category)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

CREATE TABLE article_causes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    article_id INT NOT NULL,
    cause_id INT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_article_cause (article_id, cause_id),
    INDEX idx_article_causes_article (article_id),
    INDEX idx_article_causes_cause (cause_id),
    CONSTRAINT fk_article_causes_article
        FOREIGN KEY (article_id) REFERENCES articles(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_article_causes_cause
        FOREIGN KEY (cause_id) REFERENCES causes(id)
        ON DELETE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

CREATE TABLE interpretations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(500) NOT NULL,
    authority_id INT,
    publish_date DATE,
    doc_number VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_interpretations_authority (authority_id),
    CONSTRAINT fk_interpretations_authority
        FOREIGN KEY (authority_id) REFERENCES authorities(id)
        ON DELETE SET NULL
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

CREATE TABLE article_interpretations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    article_id INT NOT NULL,
    interpretation_id INT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_article_interpretation (article_id, interpretation_id),
    INDEX idx_article_interp_article (article_id),
    INDEX idx_article_interp_interp (interpretation_id),
    CONSTRAINT fk_article_interp_article
        FOREIGN KEY (article_id) REFERENCES articles(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_article_interp_interp
        FOREIGN KEY (interpretation_id) REFERENCES interpretations(id)
        ON DELETE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

CREATE TABLE law_authorities (
    id INT AUTO_INCREMENT PRIMARY KEY,
    law_id INT NOT NULL,
    authority_id INT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_law_authority (law_id, authority_id),
    INDEX idx_law_authorities_law (law_id),
    INDEX idx_law_authorities_authority (authority_id),
    CONSTRAINT fk_law_authorities_law
        FOREIGN KEY (law_id) REFERENCES laws(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_law_authorities_authority
        FOREIGN KEY (authority_id) REFERENCES authorities(id)
        ON DELETE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
