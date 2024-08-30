-- Channel Linker {{{

CREATE TABLE links(
  linkid SERIAL PRIMARY KEY,
  name TEXT
);

CREATE TABLE channel_webhooks(
  channelid BIGINT PRIMARY KEY,
  webhookid BIGINT NOT NULL,
  token TEXT NOT NULL
);

CREATE TABLE channel_links(
  linkid INTEGER NOT NULL REFERENCES links (linkid) ON DELETE CASCADE,
  channelid BIGINT NOT NULL REFERENCES channel_webhooks (channelid) ON DELETE CASCADE,
  PRIMARY KEY (linkid, channelid)
);


-- }}}
