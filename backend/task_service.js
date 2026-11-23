const express = require('express');
const router = express.Router();
const database = require('./database');

class TaskService {
  createTask({ title, description }) {
    const tasks = database.getTasks();
    const id = tasks.length > 0 ? Math.max(...tasks.map(t => Number(t.id))) + 1 : 1;
    const task = {
      id: id,
      title: title || 'Untitled Task',
      description: description || '',
      completed: false,
      createdAt: new Date(),
      updatedAt: new Date(),
    };
    tasks.push(task);
    database.saveTasks(tasks);
    return task;
  }

  getTasks() {
    return database.getTasks();
  }

  updateTask(id, { title, description }) {
    const tasks = database.getTasks();
    const taskId = Number(id);
    const task = tasks.find((t) => Number(t.id) === taskId);
    if (!task) {
      return null;
    }
    if (title !== undefined) task.title = title;
    if (description !== undefined) task.description = description;
    task.updatedAt = new Date();
    database.saveTasks(tasks);
    return task;
  }

  deleteTask(id) {
    const tasks = database.getTasks();
    const taskId = Number(id);
    const index = tasks.findIndex((t) => Number(t.id) === taskId);
    if (index === -1) {
      return false;
    }
    tasks.splice(index, 1);
    database.saveTasks(tasks);
    return true;
  }

  toggleTaskStatus(id) {
    const tasks = database.getTasks();
    const taskId = Number(id);
    const task = tasks.find((t) => Number(t.id) === taskId);
    if (!task) {
      return null;
    }
    task.completed = !task.completed;
    task.updatedAt = new Date();
    database.saveTasks(tasks);
    return task;
  }
}

class TaskController {
  constructor() {
    this.taskService = new TaskService();
    router.post('/', this.createTask.bind(this));
    router.get('/', this.getTasks.bind(this));
    router.put('/:id', this.updateTask.bind(this));
    router.delete('/:id', this.deleteTask.bind(this));
    router.patch('/:id/toggle', this.toggleTaskStatus.bind(this));
  }

  createTask(req, res) {
    const { title, description } = req.body;
    const task = this.taskService.createTask({ title, description });
    res.status(201).json(task);
  }

  getTasks(req, res) {
    const tasks = this.taskService.getTasks();
    res.json(tasks);
  }

  updateTask(req, res) {
    const id = parseInt(req.params.id, 10);
    if (isNaN(id)) {
      return res.status(400).json({ error: 'Invalid task id' });
    }
    const { title, description } = req.body;
    const task = this.taskService.updateTask(id, { title, description });
    if (!task) {
      return res.status(404).json({ error: 'Task not found' });
    }
    res.json(task);
  }

  deleteTask(req, res) {
    const id = parseInt(req.params.id, 10);
    if (isNaN(id)) {
      return res.status(400).json({ error: 'Invalid task id' });
    }
    const success = this.taskService.deleteTask(id);
    if (!success) {
      return res.status(404).json({ error: 'Task not found' });
    }
    res.status(204).send();
  }

  toggleTaskStatus(req, res) {
    const id = parseInt(req.params.id, 10);
    if (isNaN(id)) {
      return res.status(400).json({ error: 'Invalid task id' });
    }
    const task = this.taskService.toggleTaskStatus(id);
    if (!task) {
      return res.status(404).json({ error: 'Task not found' });
    }
    res.json(task);
  }
}

new TaskController();

module.exports = router;
