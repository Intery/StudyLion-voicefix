from data import Registry, RowModel, Table
from data.columns import Integer, Bool, Timestamp, String


class LinkData(Registry):
    class Link(RowModel):
        """
        Schema
        ------
        CREATE TABLE links(
          linkid SERIAL PRIMARY KEY,
          name TEXT
        );
        """
        _tablename_ = 'links'
        _cache_ = {}

        linkid = Integer(primary=True)
        name = String()


    channel_links = Table('channel_links')

    class LinkHook(RowModel):
        """
        Schema
        ------
        CREATE TABLE channel_webhooks(
          channelid BIGINT PRIMARY KEY,
          webhookid BIGINT NOT NULL,
          token TEXT NOT NULL
        );
        """
        _tablename_ = 'channel_webhooks'
        _cache_ = {}

        channelid = Integer(primary=True)
        webhookid = Integer()
        token = String()
