from collections import defaultdict
from typing import Optional
import asyncio
from cachetools import FIFOCache

import discord
from discord.abc import GuildChannel
from discord.ext import commands as cmds
from discord import app_commands as appcmds

from meta import LionBot, LionCog, LionContext
from meta.errors import ResponseTimedOut, SafeCancellation, UserInputError
from utils.ui import Confirm

from . import logger
from .data import LinkData


async def prepare_attachments(attachments: list[discord.Attachment]):
    results = []
    for attach in attachments:
        try:
            as_file = await attach.to_file(spoiler=attach.is_spoiler())
            results.append(as_file)
        except discord.HTTPException:
            pass
    return results


async def prepare_embeds(message: discord.Message):
    embeds = [embed for embed in message.embeds if embed.type == 'rich']
    if message.reference:
        embed = discord.Embed(
            colour=discord.Colour.dark_gray(),
            description=f"Reply to {message.reference.jump_url}"
        )
        embeds.append(embed)
    return embeds



class VoiceFixCog(LionCog):
    def __init__(self, bot: LionBot):
        self.bot = bot
        self.data = bot.db.load_registry(LinkData())

        # Map of linkids to list of channelids
        self.link_channels = {}

        # Map of channelids to linkids
        self.channel_links = {}

        # Map of channelids to initialised discord.Webhook
        self.hooks = {}

        # Map of messageid to list of (channelid, webhookmsg) pairs, for updates
        self.message_cache = FIFOCache(maxsize=200)
        # webhook msgid -> orig msgid
        self.wmessages = FIFOCache(maxsize=600)

        self.lock = asyncio.Lock()


    async def cog_load(self):
        await self.data.init()

        await self.reload_links()

    async def reload_links(self):
        records = await self.data.channel_links.select_where()
        channel_links = defaultdict(set)
        link_channels = defaultdict(set)

        for record in records:
            linkid = record['linkid']
            channelid = record['channelid']

            channel_links[channelid].add(linkid)
            link_channels[linkid].add(channelid)

        channelids = list(channel_links.keys())
        if channelids:
            await self.data.LinkHook.fetch_where(channelid=channelids)
        for channelid in channelids:
            # Will hit cache, so don't need any more data queries
            await self.fetch_webhook_for(channelid)

        self.channel_links = {cid: tuple(linkids) for cid, linkids in channel_links.items()}
        self.link_channels = {lid: tuple(cids) for lid, cids in link_channels.items()}

        logger.info(
            f"Loaded '{len(link_channels)}' channel links with '{len(self.channel_links)}' linked channels."
        )

    @LionCog.listener('on_message')
    async def on_message(self, message: discord.Message):
        # Don't need this because everything except explicit messages are webhooks now
        # if self.bot.user and (message.author.id == self.bot.user.id):
        #     return
        if message.webhook_id:
            return

        async with self.lock:
            sent = []
            linkids = self.channel_links.get(message.channel.id, ())
            if linkids:
                for linkid in linkids:
                    for channelid in self.link_channels[linkid]:
                        if channelid != message.channel.id:
                            if message.attachments:
                                files = await prepare_attachments(message.attachments)
                            else:
                                files = []

                            hook = self.hooks[channelid]
                            avatar = message.author.avatar or message.author.default_avatar
                            msg = await hook.send(
                                content=message.content,
                                wait=True,
                                username=message.author.display_name,
                                avatar_url=avatar.url,
                                embeds=await prepare_embeds(message),
                                files=files,
                                allowed_mentions=discord.AllowedMentions.none()
                            )
                            sent.append((channelid, msg))
                            self.wmessages[msg.id] = message.id
                if sent:
                    # For easier lookup
                    self.wmessages[message.id] = message.id
                    sent.append((message.channel.id, message))

                    self.message_cache[message.id] = sent
                    logger.info(f"Forwarded message {message.id}")
        

    @LionCog.listener('on_message_edit')
    async def on_message_edit(self, before, after):
        async with self.lock:
            cached_sent = self.message_cache.pop(before.id, ())
            new_sent = []
            for cid, msg in cached_sent:
                try:
                    if msg.id != before.id:
                        msg = await msg.edit(
                            content=after.content,
                            embeds=await prepare_embeds(after),
                        )
                    new_sent.append((cid, msg))
                except discord.NotFound:
                    pass
            if new_sent:
                self.message_cache[after.id] = new_sent

    @LionCog.listener('on_message_delete')
    async def on_message_delete(self, message):
        async with self.lock:
            origid = self.wmessages.get(message.id, None)
            if origid:
                cached_sent = self.message_cache.pop(origid, ())
                for _, msg in cached_sent:
                    try:
                        if msg.id != message.id:
                            await msg.delete()
                    except discord.NotFound:
                        pass

    @LionCog.listener('on_reaction_add')
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        async with self.lock:
            message = reaction.message
            emoji = reaction.emoji
            origid = self.wmessages.get(message.id, None)
            if origid and reaction.count == 1:
                cached_sent = self.message_cache.get(origid, ())
                for _, msg in cached_sent:
                    # TODO: Would be better to have a Message and check the reactions
                    try:
                        if msg.id != message.id:
                            await msg.add_reaction(emoji)
                    except discord.HTTPException:
                        pass

    async def fetch_webhook_for(self, channelid) -> discord.Webhook:
        hook = self.hooks.get(channelid, None)
        if hook is None:
            row = await self.data.LinkHook.fetch(channelid)
            if row is None:
                channel = self.bot.get_channel(channelid)
                if channel is None:
                    raise ValueError("Cannot find channel to create hook.")
                hook = await channel.create_webhook(name="LabRat Channel Link")
                await self.data.LinkHook.create(
                    channelid=channelid,
                    webhookid=hook.id,
                    token=hook.token,
                )
            else:
                hook = discord.Webhook.partial(row.webhookid, row.token, client=self.bot)
            self.hooks[channelid] = hook
        return hook

    @cmds.hybrid_group(
        name='linker',
        description="Base command group for the channel linker"
    )
    @appcmds.default_permissions(manage_channels=True)
    async def linker_group(self, ctx: LionContext):
        ...

    @linker_group.command(
        name='link',
        description="Create a new link, or add a channel to an existing link."
    )
    @appcmds.describe(
        name="Name of the new or existing channel link.",
        channel1="First channel to add to the link.",
        channel2="Second channel to add to the link.",
        channel3="Third channel to add to the link.",
        channel4="Fourth channel to add to the link.",
        channel5="Fifth channel to add to the link.",
        channelid="Optionally add a channel by id (for e.g. cross-server links).",
    )
    async def linker_link(self, ctx: LionContext,
                          name: str,
                          channel1: Optional[discord.TextChannel | discord.VoiceChannel] = None,
                          channel2: Optional[discord.TextChannel | discord.VoiceChannel] = None,
                          channel3: Optional[discord.TextChannel | discord.VoiceChannel] = None,
                          channel4: Optional[discord.TextChannel | discord.VoiceChannel] = None,
                          channel5: Optional[discord.TextChannel | discord.VoiceChannel] = None,
                          channelid: Optional[str] = None,
                          ):
        if not ctx.interaction:
            return
        await ctx.interaction.response.defer(thinking=True)

        # Check if link 'name' already exists, create if not
        existing = await self.data.Link.fetch_where()
        link_row = next((row for row in existing if row.name.lower() == name.lower()), None)
        if link_row is None:
            # Create
            link_row = await self.data.Link.create(name=name)
            link_channels = set()
            created = True
        else:
            records = await self.data.channel_links.select_where(linkid=link_row.linkid)
            link_channels = {record['channelid'] for record in records}
            created = False

        # Create webhooks and webhook rows on channels if required
        maybe_channels = [
            channel1, channel2, channel3, channel4, channel5,
        ]
        if channelid and channelid.isdigit():
            channel = self.bot.get_channel(int(channelid))
            maybe_channels.append(channel)

        channels = [channel for channel in maybe_channels if channel]
        for channel in channels:
            await self.fetch_webhook_for(channel.id)

        # Insert or update the links
        for channel in channels:
            if channel.id not in link_channels:
                await self.data.channel_links.insert(linkid=link_row.linkid, channelid=channel.id)

        await self.reload_links()

        if created:
            embed = discord.Embed(
                colour=discord.Colour.brand_green(),
                title="Link Created",
                description=(
                    "Created the link **{name}** and linked channels:\n{channels}"
                ).format(name=name, channels=', '.join(channel.mention for channel in channels))
            )
        else:
            channelids = self.link_channels[link_row.linkid]
            channelstr = ', '.join(f"<#{cid}>" for cid in channelids)
            embed = discord.Embed(
                colour=discord.Colour.brand_green(),
                title="Channels Linked",
                description=(
                    "Updated the link **{name}** to link the following channels:\n{channelstr}"
                ).format(name=link_row.name, channelstr=channelstr)
            )
        await ctx.reply(embed=embed)

    @linker_group.command(
        name='unlink',
        description="Destroy a link, or remove a channel from a link."
    )
    @appcmds.describe(
        name="Name of the link to destroy",
        channel="Channel to remove from the link.",
    )
    async def linker_unlink(self, ctx: LionContext,
                            name: str, channel: Optional[GuildChannel] = None):
        if not ctx.interaction:
            return
        # Get the link, error if it doesn't exist
        existing = await self.data.Link.fetch_where()
        link_row = next((row for row in existing if row.name.lower() == name.lower()), None)
        if link_row is None:
            raise UserInputError(
                f"Link **{name}** doesn't exist!"
            )

        link_channelids = self.link_channels.get(link_row.linkid, ())

        if channel is not None:
            # If channel was given, remove channel from link and ack
            if channel.id not in link_channelids:
                raise UserInputError(
                    f"{channel.mention} is not linked in **{link_row.name}**!"
                )
            await self.data.channel_links.delete_where(channelid=channel.id, linkid=link_row.linkid)
            embed = discord.Embed(
                colour=discord.Colour.brand_green(),
                title="Channel Unlinked",
                description=f"{channel.mention} has been removed from **{link_row.name}**."
            )
        else:
            # Otherwise, confirm link destroy, delete link row, and ack
            channels = ', '.join(f"<#{cid}>" for cid in link_channelids)
            confirm = Confirm(
                f"Are you sure you want to remove the link **{link_row.name}**?\nLinked channels: {channels}",
                ctx.author.id,
            )
            confirm.embed.colour = discord.Colour.red()
            try:
                result = await confirm.ask(ctx.interaction)
            except ResponseTimedOut:
                result = False
            if not result:
                raise SafeCancellation

            embed = discord.Embed(
                colour=discord.Colour.brand_green(),
                title="Link removed",
                description=f"Link **{link_row.name}** removed, the following channels were unlinked:\n{channels}"
            )
            await link_row.delete()

        await self.reload_links()
        await ctx.reply(embed=embed)

    @linker_link.autocomplete('name')
    async def _acmpl_link_name(self, interaction: discord.Interaction, partial: str):
        """
        Autocomplete an existing link.
        """
        existing = await self.data.Link.fetch_where()
        names = [row.name for row in existing]
        matching = [row.name for row in existing if partial.lower() in row.name.lower()]
        if not matching:
            choice = appcmds.Choice(
                name=f"Create a new link '{partial}'",
                value=partial
            )
            choices = [choice]
        else:
            choices = [
                appcmds.Choice(
                    name=f"Link {name}",
                    value=name
                )
                for name in matching
            ]
        return choices

    @linker_unlink.autocomplete('name')
    async def _acmpl_unlink_name(self, interaction: discord.Interaction, partial: str):
        """
        Autocomplete an existing link.
        """
        existing = await self.data.Link.fetch_where()
        matching = [row.name for row in existing if partial.lower() in row.name.lower()]
        if not matching:
            choice = appcmds.Choice(
                name=f"No existing links matching '{partial}'",
                value=partial
            )
            choices = [choice]
        else:
            choices = [
                appcmds.Choice(
                    name=f"Link {name}",
                    value=name
                )
                for name in matching
            ]
        return choices

    @linker_group.command(
        name='links',
        description="Display the existing channel links."
    )
    async def linker_links(self, ctx: LionContext):
        if not ctx.interaction:
            return
        await ctx.interaction.response.defer(thinking=True)

        links = await self.data.Link.fetch_where()

        if not links:
            embed = discord.Embed(
                colour=discord.Colour.light_grey(),
                title="No channel links have been set up!",
                description="Create a new link and add channels with {linker}".format(
                    linker=self.bot.core.mention_cmd('linker link')
                )
            )
        else:
            embed = discord.Embed(
                colour=discord.Colour.brand_green(),
                title=f"Channel Links in {ctx.guild.name}",
            )
            for link in links:
                channelids = self.link_channels.get(link.linkid, ())
                channelstr = ', '.join(f"<#{cid}>" for cid in channelids)
                embed.add_field(
                    name=f"Link **{link.name}**",
                    value=channelstr,
                    inline=False
                )
        # TODO: May want paging if over 25 links....
        await ctx.reply(embed=embed)

    @linker_group.command(
        name="webhook",
        description='Manually configure the webhook for a given channel.'
    )
    async def linker_webhook(self, ctx: LionContext, channel: discord.abc.GuildChannel, webhook: str):
        if not ctx.interaction:
            return

        hook = discord.Webhook.from_url(webhook, client=self.bot)
        existing = await self.data.LinkHook.fetch(channel.id)
        if existing:
            await existing.update(webhookid=hook.id, token=hook.token)
        else:
            await self.data.LinkHook.create(
                channelid=channel.id,
                webhookid=hook.id,
                token=hook.token,
            )
        self.hooks[channel.id] = hook
        await ctx.reply(f"Webhook for {channel.mention} updated!")
