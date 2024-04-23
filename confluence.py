import os
import re
import json
import discord
import asyncio
import datetime
import openai

from discord import app_commands
from discord.ui import Button, View, TextInput, Modal
from discord.ext import commands

from communex.module.module import Module
from communex.module.server import ModuleServer
from communex.compat.key import classic_load_key

discord_key = os.getenv("DISCORD_BOT_KEY")
openai.api_key = os.getenv("OPENAI_API_KEY")

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix='/', intents=intents)

# Load the required roles from the JSON file
try:
    with open("required_roles.json", "r") as file:
        required_roles = json.load(file)
except FileNotFoundError:
    required_roles = {}

class AskModal(Modal, title="Ask Modal"):
    answer = TextInput(label="Answer", max_length=400, style=discord.TextStyle.long)
    end_time = None

    def add_view(self, question, view: View, end_time):
        self.answer.placeholder = question[0:100]
        self.view = view
        self.end_time = end_time

    async def on_submit(self, interaction: discord.Interaction):
        time_remaining = (self.end_time - datetime.datetime.now()).total_seconds()
        minutes_remaining = max(0, int(time_remaining // 60))
        embed = discord.Embed(title="Thank You for Voting", description=f"Answers will be summarized in approximately {minutes_remaining} minutes.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        self.view.stop()

def response_view(modal_text="default text", modal_label="Response", button_label="Answer", timeout=2700.0):
    async def view_timeout():
        modal.stop()

    view = View()
    view.on_timeout = view_timeout
    view.timeout = timeout
    view.auto_defer = True

    modal = AskModal(title=modal_label)
    modal.auto_defer = True
    modal.timeout = timeout

    async def button_callback(interaction):
        answer = await interaction.response.send_modal(modal)

    button = Button(label=button_label, style=discord.ButtonStyle.blurple)
    button.callback = button_callback
    view.add_item(button)

    end_time = datetime.datetime.now() + datetime.timedelta(seconds=timeout)
    modal.add_view(modal_text, view, end_time)

    return view, modal

def split_text_into_chunks(text, max_chunk_size=2000):
    num_chunks = max(1, (len(text) + max_chunk_size - 1) // max_chunk_size)
    chunk_size = (len(text) + num_chunks - 1) // num_chunks
    text_chunks = []
    start_index = 0

    while start_index < len(text):
        end_index = start_index + chunk_size

        if end_index < len(text):
            boundary_index = text.rfind(".", start_index, end_index) + 1
            if boundary_index > start_index:
                end_index = boundary_index

        text_chunks.append(text[start_index:end_index])
        start_index = end_index

    return text_chunks

def redo_view(interaction, prompt, question):
    async def button_callback(interaction):
        await interaction.response.defer()

        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are an AI language model that summarizes responses to a given question."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=500,
            temperature=0.6
        )

        response_text = response.choices[0].message.content.strip()
        embed = discord.Embed(title="Consensus", description=f"**Question**\n{question}\n\n**Consensus**\n{response_text}")

        await interaction.followup.send(embed=embed)

    view = View()
    view.timeout = None
    button = Button(label="Redo", style=discord.ButtonStyle.blurple)
    button.callback = button_callback
    view.add_item(button)

    return view

async def get_guild_members(guild, target_names=None):
    members = []

    if target_names:
        for name in target_names:
            member = discord.utils.get(guild.members, display_name=name)
            if member:
                members.append(member)
    else:
        members = guild.members

    return members

class AskGroup(Module):
    def __init__(self):
        super().__init__()

    @bot.event
    async def on_ready(self):
        await bot.tree.sync()
        print("Ask Group Bot is online")

    @bot.event
    async def on_close(self):
        print("Ask Group Bot is offline")

    @bot.tree.command(name="set_ask_group_role", description="Set the required role for the ask_group command")
    @app_commands.describe(role="The role required to use the ask_group command")
    async def set_ask_group_role(self, interaction: discord.Interaction, role: discord.Role):
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(title="Access Denied", description="You must be a server administrator to use this command.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        required_roles[str(interaction.guild.id)] = str(role.id)

        # Save the updated required roles to the JSON file
        with open("required_roles.json", "w") as file:
            json.dump(required_roles, file)

        embed = discord.Embed(title="Role Set", description=f"The required role for the ask_group command has been set to '{role.name}'.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="ask_group", description="Ask group a question and auto-summarize")
    @app_commands.describe(
        question="The question to ask the group",
        target="The target users to ask the question (optional)",
        timeout="The timeout duration in minutes (optional, default is 45 minutes)"
    )
    async def ask_group(self, interaction: discord.Interaction, question: str, target: str = None, timeout: int = 45):
        if len(question) == 0:
            return

        timeout_seconds = timeout * 60

        # Get Relevant Users
        guild = interaction.guild
        target_names = [name.strip().lstrip('@') for name in target.split()] if target else None

        if str(guild.id) in required_roles:
            role_id = int(required_roles[str(guild.id)])
            access_role = guild.get_role(role_id)
            if access_role not in interaction.user.roles:
                embed = discord.Embed(title="Access Denied", description=f"You don't have the required role '{access_role.name}' to use this command.")
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        members = await get_guild_members(guild, target_names)

        if interaction.user not in members:
            members.append(interaction.user)

        # Get people in Guild
        responses = []
        views = []

        # Calculate the end time of the voting period
        end_time = datetime.datetime.now() + datetime.timedelta(seconds=timeout_seconds)
        formatted_end_time = end_time.strftime("%Y-%m-%d %H:%M:%S")

        embed = discord.Embed(title="Confluence Experiment", description=question)
        embed.add_field(name="Time Limit", value=f"Please reply within {timeout} minutes. The voting period ends at {formatted_end_time}.")

        # Message Users
        for person in members:
            view, modal = response_view(modal_text=question, timeout=timeout_seconds)
            try:
                if person == interaction.user:
                    if isinstance(interaction.channel, discord.TextChannel):
                        await interaction.response.send_message("Polling Initiated", ephemeral=True)
                        await person.send(embed=embed, view=view)
                    else:
                        await interaction.response.send_message(embed=embed, view=view)
                else:
                    await person.send(embed=embed, view=view)
            except:
                continue

            responses.append(modal)
            views.append(view)

        # Gather Answers
        all_text = []

        for response in responses:
            await response.wait()
            all_text.append(response.answer.value)

        joined_answers = ""

        for t in all_text:
            if t is not None:
                joined_answers += t + "\n\n"

        if len(joined_answers.strip()) == 0:
            j_embed = discord.Embed(title="No Responses", description=f"No responses provided to summarize")
            await interaction.followup.send(embed=j_embed)
            return

        if len(responses) == 1:
            k_embed = discord.Embed(title="One Response", description=all_text[0])
            await interaction.followup.send(embed=k_embed)
            return

        # Query GPT-4
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are an AI language model that summarizes responses to a given question."},
                {"role": "user", "content": f"Question: {question}\n\nResponses:\n{joined_answers}\n\nPlease provide a detailed summary and consensus of the responses."}
            ],
            max_tokens=500,
            temperature=0.6
        )
        response_text = response.choices[0].message.content.strip()

        # Send Results to People
        a_embed = discord.Embed(title="Responses", description=f"{joined_answers}")
        embed = discord.Embed(title="Consensus (beta)", description=f"**Question**\n{question}\n\n**Consensus**\n{response_text}")

        for person in members:
            try:
                await person.send("Responses", embed=a_embed)
                await person.send("Consensus", embed=embed)
            except:
                continue

        # Send a Redo Option
        r_view = redo_view(interaction, f"Question: {question}\n\nResponses:\n{joined_answers}\n\nPlease provide a detailed summary and consensus of the responses.", question)
        await interaction.followup.send(view=r_view)

if __name__ == "__main__":

    import uvicorn

    model = AskGroup()
    key = classic_load_key("confluence_key")
    model_server = ModuleServer(model, key)
    app = model_server.get_fastapi_app()

    uvicorn.run(app, host="127.0.0.1", port=8000)
    bot.run(discord_key)